/* Display physical profiles; retain member.profile for routing and form values. */
function memberLabelForProfile(profile, members) {
  const member = members.find(item => item.profile === profile);
  return member ? memberLabel(member) : profile;
}
function memberHostProfile(member) {
  return member.host_profile || member.profile;
}
function memberDisplayName(member) {
  return member.display_name || memberHostProfile(member);
}
function memberLabel(member) {
  const name = memberDisplayName(member), profile = memberHostProfile(member);
  return name === profile ? profile : `${name}（${profile}）`;
}

// Textareas display the shared member label and retain each selected handle.
// Bind by position so even identical labels can address different Bots.
const memberMentionDrafts = new WeakMap();
function syncMemberMentions(ta, edit = null) {
  let draft = memberMentionDrafts.get(ta);
  if (!draft) {
    draft = {text: ta.value, mentions: []};
    memberMentionDrafts.set(ta, draft);
  }
  if (draft.text === ta.value && !edit) return draft;
  const old = draft.text, next = ta.value;
  if (edit && edit.start === edit.end && old.length > next.length) {
    const removed = old.length - next.length;
    if (edit.type === 'deleteContentBackward') edit = {...edit, start: edit.start - removed};
    if (edit.type === 'deleteContentForward') edit = {...edit, end: edit.end + removed};
  }
  let start = 0, end = old.length, nextEnd = next.length;
  if (edit && edit.text === old && old.slice(0, edit.start) === next.slice(0, edit.start)
      && next.endsWith(old.slice(edit.end))) {
    start = edit.start;
    end = edit.end;
    nextEnd = next.length - (old.length - end);
  } else {
    while (start < old.length && start < next.length && old[start] === next[start]) start++;
    while (end > start && nextEnd > start && old[end - 1] === next[nextEnd - 1]) { end--; nextEnd--; }
  }
  const delta = nextEnd - end;
  draft.mentions = draft.mentions.flatMap(mention => {
    if (mention.end <= start) return [mention];
    if (mention.start >= end) return [{...mention, start: mention.start + delta, end: mention.end + delta}];
    return []; // Editing a selected label releases its old identity.
  });
  draft.text = next;
  return draft;
}

function bindMemberMentions(ta) {
  let edit = null;
  const history = new Map();
  const remember = () => {
    const draft = syncMemberMentions(ta);
    history.set(draft.text, draft.mentions.map(m => ({...m})));
    if (history.size > 100) history.delete(history.keys().next().value);
  };
  ta.addEventListener('beforeinput', event => {
    remember();
    syncMemberMentions(ta);
    edit = {text: ta.value, start: ta.selectionStart, end: ta.selectionEnd, type: event.inputType};
  });
  ta.addEventListener('input', event => {
    if (['historyUndo', 'historyRedo'].includes(event.inputType) && history.has(ta.value)) {
      memberMentionDrafts.set(ta, {text: ta.value, mentions: history.get(ta.value).map(m => ({...m}))});
    } else syncMemberMentions(ta, edit);
    edit = null;
  });
}

function insertMemberMention(ta, member, start, end, project) {
  syncMemberMentions(ta);
  const old = ta.value, label = '@' + memberLabel(member);
  ta.value = old.slice(0, start) + label + ' ' + old.slice(end);
  const draft = syncMemberMentions(ta, {text: old, start, end});
  draft.mentions.push({start, end: start + label.length, label, profile: member.profile, project});
  draft.mentions.sort((a, b) => a.start - b.start);
  return start + label.length + 1;
}

function serializeMemberMentions(ta, project, members) {
  const draft = syncMemberMentions(ta);
  let text = ta.value;
  for (const mention of [...draft.mentions].reverse()) {
    if (mention.project !== project || !members.some(m => m.profile === mention.profile))
      throw new Error('点名成员已不在当前范围，请重新选择 @ 成员');
    text = text.slice(0, mention.start) + '@' + mention.profile + text.slice(mention.end);
  }
  return text.trim();
}
