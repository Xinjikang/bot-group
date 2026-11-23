"""Recipient-specific message semantics shared by reads and scheduling."""
import json


# Bind this predicate to the recipient's inbox row (alias i).
AWAITING_DELIVERY = """NOT EXISTS (
    SELECT 1 FROM hermes_executions e WHERE e.execution_id=i.relay_execution_id
    AND (e.state!='terminal' OR e.outcome='completed'))"""


def effective_need_reply(message, recipient):
    """Return the sender's requirement only for an explicitly mentioned recipient."""
    mentions = message["mentions"] or []
    if isinstance(mentions, str):
        try:
            mentions = json.loads(mentions)
        except (TypeError, ValueError):
            mentions = []
    if not isinstance(mentions, list) or recipient not in mentions:
        return None
    return message["need_reply"]
