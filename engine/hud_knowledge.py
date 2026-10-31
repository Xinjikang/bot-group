"""Versioned preparation guidance and bounded installed-product usage knowledge."""
from pathlib import Path
import json
import re

from . import hud_inspection, project_templates

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    'display_name': '面向用户的项目名称；稳定项目 ID 由系统维护。',
    'description': '项目目标、输入资料、交付成果和范围，结合项目类型填写。',
    'root': '引擎所在机器的项目主目录绝对路径；新建与导入按向导检查目录。',
    'template': '根据工作目标选择场景模板；创建后的模板类型作为当前参考。',
    'directory_mode': 'shared 共用主目录串行修改；worktree 使用 Git 独立工作区。',
    'worktree_root': '独立工作区根目录，须与项目主目录分开。',
}
USAGE_DOCS = (
    'docs/engineering/PROJECT-HUD.md', 'docs/engineering/PROJECT-SETUP-WIZARD-2026-10-14.md',
    'docs/engineering/PROJECT-CONFIGURATION.md', 'docs/engineering/TESTING-TEMPLATE.md',
    'docs/engineering/REQ104-SCENARIO-TEMPLATES-2026-10-10.md',
    'docs/engineering/WORKSPACE-RESOURCE-ENTRY-2026-10-15.md',
    'docs/engineering/TASK-BRIEF-CONTRACT.md', 'docs/guides/RELEASE-GUIDE.md',
)


def usage(history):
    query = ' '.join(str(h.get('text', '')) for h in history[-3:])[-2400:]
    terms = re.findall(r'[a-zA-Z][\w.-]{2,}', query)
    for chunk in re.findall(r'[\u4e00-\u9fff]+', query):
        terms.extend(chunk[i:i+2] for i in range(len(chunk)-1))
    terms = list(dict.fromkeys(t.lower() for t in terms))[:64]
    names = list(USAGE_DOCS)
    try:
        index = (ROOT / 'docs/INDEX.md').read_text(encoding='utf-8-sig')
        for link in hud_inspection.LINK.findall(index):
            path = hud_inspection.local_link(ROOT, 'docs/INDEX.md', link)
            if path and path.suffix == '.md' and path.parts[:2] in (
                    ('docs', 'engineering'), ('docs', 'guides')):
                names.append(path.as_posix())
    except (OSError, UnicodeError):
        pass
    candidates = []
    for name in dict.fromkeys(names):
        try:
            if not hud_inspection.safe_path(ROOT, ROOT / name) or (ROOT / name).stat().st_size > 150000:
                continue
            content = (ROOT / name).read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            continue
        lower = content.lower()
        score = sum(min(lower.count(term), 8) for term in terms)
        candidates.append((score, name, content))
    candidates.sort(key=lambda x: -x[0])
    documents = []
    for _, name, content in candidates[:4]:
        sections = re.split(r'(?=^## )', content, flags=re.M)
        ranked = sorted(enumerate(sections), key=lambda p: -sum(p[1].lower().count(t.lower()) for t in terms))
        chosen, budget = [], 7000
        for index, section in ranked:
            if budget <= 0:
                break
            excerpt = section[:max(0, budget-1)]
            chosen.append((index, excerpt))
            budget -= len(excerpt)+1
        text = '\n'.join(s for _, s in sorted(chosen))
        documents.append({'path': name, 'text': text, 'truncated': len(text) < len(content),
                          'source': 'installed_bot_group_documentation'})
    return {'source': 'current_installation', 'documents': documents,
            'coverage': '相关章节摘录；省略章节和外部系统的行为需要进一步核实'}


def guidance(template):
    if template['id'] == 'blank':
        return {'inputs': ['目标、现有资料、交付成果和验收方式'], 'documents': [],
                'configuration': '结合实际项目组织资料，逐步形成约定。'}
    return json.loads(project_templates.text_resource(
        project_templates.TEMPLATE_ROOT / template['id'], 'assistant.json'))


def preparation(project, template, materials):
    guide = guidance(template)
    result = {'template': template['id'], 'guidance': guide, 'configuration_fields': FIELDS,
              'documents': [], 'source': 'template_assistant_guidance',
              'scope': '启动资料；阶段产物按项目流程完善。项目现有结构与用户约定优先。'}
    if not project.get('root') or not Path(project['root']).is_dir():
        result['status'] = 'draft_or_unavailable'
        return result
    base = Path(project['root']).resolve()
    for expected in guide['documents']:
        name = expected['path']
        # Imported projects may retain their established doc/ directory.
        if name.startswith('docs/') and not (base / name).exists() and (base / 'doc').is_dir() and not (base / 'docs').exists():
            name = 'doc/' + name[5:]
        item = {**expected, 'path': name, 'status': 'unknown', 'pending': []}
        path = base / name
        if not hud_inspection.safe_path(base, path):
            item['status'] = 'unknown'
        elif not path.exists():
            item['status'] = 'missing'
        elif name not in materials['documents'] or materials['documents'][name]['truncated']:
            item['status'] = 'unknown'
        else:
            text = materials['documents'][name]['text']
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r'待(?:用户|填写|提供|确认|配置|选择|登记|补充)|\bTODO\b|\bTBD\b|\{\{[A-Z_]+\}\}', line):
                    item['pending'].append({'line': number, 'text': line[:350]})
            item['pending'] = item['pending'][:20]
            source = template.get('contents', {}).get(expected['path'], '')
            skeleton = [line.strip() for line in source.splitlines()
                        if len(line.strip()) > 20 and '{{' not in line and not line.startswith('#')]
            unchanged = sum(line in text for line in skeleton)
            item['status'] = ('incomplete' if not text.strip() or item['pending'] else
                              'needs_review' if skeleton and unchanged == len(skeleton) else 'present')
            item['sha256'] = materials['documents'][name]['sha256']
        result['documents'].append(item)
    return result
