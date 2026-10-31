"""Model-safe projection and retirement of historical project-note proposals."""
import copy

NOTE_FIELDS = {'facts', 'before_facts', 'legacy_note_proposals'}


def contains_notes(value):
    if isinstance(value, dict):
        return any(key in NOTE_FIELDS and bool(child) or contains_notes(child) for key, child in value.items())
    return isinstance(value, list) and any(contains_notes(child) for child in value)


def project(value):
    if isinstance(value, list):
        return [project(child) for child in value]
    if not isinstance(value, dict):
        return value
    notes = contains_notes(value)
    result = {key: project(child) for key, child in value.items() if key not in NOTE_FIELDS}
    if notes:
        for key in ('text', 'reason', 'guide', 'notification', 'question'):
            result.pop(key, None)
    proposal = result.get('proposal')
    if proposal and not proposal.get('changes') and not proposal.get('documents') and contains_notes(value.get('proposal')):
        result.pop('proposal')
    if 'cards' in result:
        result['cards'] = [card for card in result['cards'] if card.get('proposal') or card.get('question')]
    return result


def retire(item):
    state = item['state']
    proposal, cards = state.get('proposal'), state.get('cards', [])
    if not contains_notes(proposal) and not contains_notes(cards):
        return False
    archive = state.setdefault('legacy_note_proposals', [])
    if contains_notes(proposal):
        archive.append(copy.deepcopy({'proposal': proposal}))
        cleaned = project({'proposal': proposal}).get('proposal')
        if cleaned:
            state['proposal'] = cleaned
        else:
            state.pop('proposal', None)
    for card in cards:
        if contains_notes(card):
            archive.append(copy.deepcopy(card))
    state['cards'] = project({'cards': cards})['cards']
    state['legacy_note_revision'] = state.get('legacy_note_revision', 0) + 1
    pending = state.get('question') or state.get('proposal') or state['cards']
    state['notification'] = {'kind': 'pending' if pending else 'none'}
    if item['status'] == 'waiting' and not pending:
        item['status'] = 'idle'
    return True
