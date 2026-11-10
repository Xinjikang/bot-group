"""Structured process waits and trusted native process-notification identities."""
import json
import math

PREFIX = 'process:'
SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_background_callbacks (
    execution_id TEXT NOT NULL, admission_id TEXT NOT NULL,
    wait_id TEXT NOT NULL, settles INTEGER NOT NULL,
    PRIMARY KEY(execution_id, admission_id, wait_id)
);
"""


def process_updates(messages):
    """Yield the final notification contract observed for each tool-owned process."""
    states = {}
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        if message.get('name', message.get('tool_name')) not in ('terminal', 'process_manage'):
            continue
        try:
            value = json.loads(message.get('content', ''))
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        pid = value.get('session_id')
        if not isinstance(pid, str) or not pid.startswith('proc_'):
            continue
        if value.get('status') in ('exited', 'completed', 'killed', 'failed', 'error', 'timeout'):
            states[PREFIX + pid] = False
        elif value.get('notify_on_complete') is True:
            states[PREFIX + pid] = True
        elif value.get('notify_on_complete') is False:
            states[PREFIX + pid] = False
    return states


def identity(value):
    try:
        parts = json.loads(value)
    except (TypeError, ValueError):
        return None
    if (not isinstance(parts, list) or len(parts) != 3 or not isinstance(parts[1], str)
            or not parts[1].startswith('proc_')):
        return None
    kind, pid, token = parts
    if kind == 'completion':
        if type(token) not in (int, float):
            return None
        try:
            if not math.isfinite(token):
                return None
        except OverflowError:
            return None
    elif kind in ('watch_match', 'watch_disabled', 'heartbeat'):
        if not isinstance(token, str) or not token:
            return None
    else:
        return None
    return kind, pid


def callback_waits(item, principal, payload, process_ids):
    """Only authority-persisted automation descriptors attest a process callback."""
    if not isinstance(principal, str) or not principal.startswith('automation:'):
        return []
    if not isinstance(payload, str) or len(payload) > 1024 * 1024:
        return []
    try:
        value = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(value, dict):
        return []
    descriptor = value.get('local_automation_v1')
    if descriptor is None:
        native_text = value.get('native_text_v1')
        descriptor = native_text.get('automation') if isinstance(native_text, dict) else None
    if not isinstance(descriptor, dict):
        return []
    identities = descriptor.get('identities', [descriptor.get('identity')])
    if not isinstance(identities, list) or item['request_id'] not in identities:
        return []
    settled = {}
    for raw in identities:
        parsed = identity(raw)
        if parsed and parsed[1] in process_ids:
            key = PREFIX + parsed[1]
            settled[key] = settled.get(key, False) or parsed[0] == 'completion'
    return [(key, int(done)) for key, done in settled.items()]
