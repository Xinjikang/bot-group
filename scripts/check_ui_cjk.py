"""Static pre-commit check for hardcoded CJK UI copy (req#178 task#333).

Scans frontend/*.js and frontend/index.html for lines containing CJK
characters, excluding:

- comment-only lines (JS // and /* */, HTML <!-- -->)
- CJK that only appears inside a trailing // or /* */ comment on a code line
- the i18n dictionary values (frontend/i18n.js zh fields)
- lines that already route the text through t()/window.t(...) or a
  data-i18n / data-i18n-* attribute (the CJK there is a fallback/default)
- the bilingual language entry (语言 / Language) and language names shown in
  the language dialog (简体中文 / English) — those are intentional
- server-data defaults (S.userDisplayName initial value)

Any remaining line is a candidate for user-visible Chinese in English mode
and must be migrated before commit. Exit code 1 when candidates remain.

Run: py -3.10 -X utf8 -B scripts/check_ui_cjk.py
"""
import re
import sys
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
CJK = re.compile(r'[\u4e00-\u9fff]')

# Intentional bilingual/data entries that must stay Chinese.
EXEMPT_LINES = (
    '语言 / Language',
    '简体中文',
    'userDisplayName: "用户"',
    '[互@断链]',
    '[收口]',
)


def strip_trailing_comments(line):
    """Drop trailing // and /* */ comments so CJK inside them is ignored."""
    out = line
    for opener, closer in (('//', None), ('/*', '*/')):
        start = out.find(opener)
        while start != -1:
            if closer:
                end = out.find(closer, start + 2)
                if end == -1:
                    out = out[:start]  # unclosed block comment: drop the rest
                    break
                out = out[:start] + out[end + len(closer):]
            else:
                out = out[:start]
            start = out.find(opener)
    return out


# Attribute-level check: title/aria-label/placeholder values containing CJK
# must carry the matching data-i18n marker on the same element (same line for
# static HTML; JS dynamic attributes are covered by the line-level t() rule).
ATTR_RULES = (
    (re.compile(r'\btitle="([^"]*)"'), 'data-i18n-title'),
    (re.compile(r'\baria-label="([^"]*)"'), 'data-i18n-aria-label'),
    (re.compile(r'\bplaceholder="([^"]*)"'), 'data-i18n-placeholder'),
)


def attribute_hits(line):
    found = []
    for pattern, marker in ATTR_RULES:
        for match in pattern.finditer(line):
            if CJK.search(match.group(1)) and marker not in line:
                found.append(f'{marker} missing for value: {match.group(1)[:60]}')
    return found


def is_comment_only(line):
    stripped = line.strip()
    return (stripped.startswith('//') or stripped.startswith('/*')
            or stripped.startswith('*') or stripped.startswith('<!--')
            or stripped.startswith('#') or stripped.startswith('-->'))


def main():
    hits = []
    for path in sorted(list(FRONTEND.glob('*.js')) + [FRONTEND / 'index.html']):
        in_block_comment = False
        for lineno, line in enumerate(path.read_text(encoding='utf-8').split('\n'), 1):
            # Track /* */ and <!-- --> blocks on every line, not only CJK ones.
            if in_block_comment:
                if '*/' in line or '-->' in line:
                    in_block_comment = False
                continue
            stripped = line.lstrip()
            if stripped.startswith('/*'):
                in_block_comment = '*/' not in line
            elif stripped.startswith('<!--'):
                in_block_comment = '-->' not in line
            if not CJK.search(line):
                continue
            if path.name == 'i18n.js':
                continue  # dictionary values live here
            if is_comment_only(line):
                continue
            if any(mark in line for mark in EXEMPT_LINES):
                continue
            if not CJK.search(strip_trailing_comments(line)):
                continue  # CJK only in trailing comments
            if re.search(r'\bt\s*\(', line) or 'data-i18n' in line:
                # Text routed through the dictionary; still check attribute
                # values that carry CJK without their own data-i18n marker.
                for detail in attribute_hits(line):
                    hits.append(f'{path.name}:{lineno}: [{detail}] {line.strip()[:100]}')
                continue
            hits.append(f'{path.name}:{lineno}: {line.strip()[:120]}')
    if hits:
        print(f'{len(hits)} candidate line(s) with hardcoded CJK copy:')
        for hit in hits:
            print('  ' + hit)
        return 1
    print('PASS: no hardcoded CJK UI copy outside the dictionary')
    return 0


if __name__ == '__main__':
    sys.exit(main())
