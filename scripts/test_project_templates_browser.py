"""Current wizard and legacy project latest SOUL references in isolated Chrome."""
import argparse
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_project_templates import TemplateTests
from scripts.test_project_import import fingerprints

ROOT = Path(__file__).resolve().parents[1]


def main(evidence_path=None, screenshot_path=None):
    fixture = TemplateTests(); fixture.setUp()
    try:
        before = fingerprints(fixture.hosts)
        state = {'failure': False, 'config_saves': [], 'stale': False}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width': 1280, 'height': 950}, permissions=['clipboard-read', 'clipboard-write'])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route(request):
                req = request.request; path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain'); return
                if 'config' in path and req.method in ('PUT', 'PATCH', 'POST'):
                    state['config_saves'].append(path)
                if path.startswith('/projects/templates/') and state['failure']:
                    request.fulfill(status=503, json={'detail': 'reference temporarily unavailable'}); return
                if path.startswith('/projects/') or path in ('/projects', '/members'):
                    response = fixture.client.request(req.method, req.url, json=req.post_data_json if req.method == 'POST' else None)
                    data = response.json()
                    if path.startswith('/projects/templates/') and state['stale']:
                        data['version'] = 'changed'
                    request.fulfill(status=response.status_code, json=data); return
                request.fulfill(json={'threads': [], 'messages': [], 'tasks': [], 'requirements': [], 'pending_approval': []})
            page.route('**/*', route)
            page.goto('https://workspace.test/app/index.html')
            page.evaluate('showLogin(false)')
            assert page.locator('#dlg-project-initialization').count() == 0
            assert page.evaluate('typeof openLegacyProjectInitialization') == 'undefined'
            for mode, name in (('new', 'software'), ('import', 'research'), ('import', 'writing'),
                               ('import', 'product-prototype'), ('new', 'product-prototype'),
                               ('import', 'algorithm'), ('new', 'algorithm'),
                               ('import', 'testing'), ('new', 'testing')):
                page.evaluate('(mode) => openProjectImport({mode})', mode)
                root = fixture.base / (name + '-browser') if name in ('product-prototype', 'algorithm', 'testing') else fixture.root
                if mode == 'import' and name in ('product-prototype', 'algorithm', 'testing'):
                    root.mkdir()
                page.locator('#import-root').fill(str(root))
                page.locator('#import-template').select_option(name)
                page.locator('#import-preview').click()
                page.locator('#import-config').wait_for(state='visible')
                page.locator('#import-next').click()
                page.locator('#import-role-step').wait_for(state='visible')
                assert '第 3 步' in page.locator('#import-step').inner_text()
                page.locator('#import-soul > summary').click()
                refs = page.locator('#import-soul-content')
                refs.locator('details').first.wait_for(state='visible')
                detail = fixture.client.get('/projects/templates/' + name).json()
                assert refs.locator('details').count() == len(detail['references'])
                refs.locator('summary').first.click()
                content = refs.locator('pre').first.text_content()
                assert content == detail['references'][0]['content']
                refs.locator('.template-copy').first.click()
                page.wait_for_function("document.querySelector('#import-soul-content [role=status]').textContent === '已复制'")
                assert page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n') == content.replace('\r\n', '\n')
                with page.expect_download() as download:
                    refs.locator('.template-download').first.click()
                assert Path(download.value.path()).read_text(encoding='utf-8') == content
                assert download.value.suggested_filename == f"{name}-{detail['version']}-{detail['references'][0]['role_id']}.md"
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#dlg-project-import').evaluate('d => d.scrollWidth <= d.clientWidth')
                if screenshot_path: page.screenshot(path=screenshot_path)
                if mode == 'new' and name in ('product-prototype', 'algorithm', 'testing'):
                    page.locator('#import-next').click()
                    page.locator('#import-agents-step').wait_for(state='visible')
                    draft = page.locator('#import-agents-content').input_value()
                    if name == 'product-prototype':
                        assert 'UX 设计师开展用户研究' in draft
                        assert '修订完成后回到原型评审员复审' in draft
                    elif name == 'algorithm':
                        assert '算法研究员调研一手文献' in draft
                        assert '修订完成后回到评审员复审' in draft
                        assert '源码、Demo、配置、数据和模型目录由用户管理' in draft
                    else:
                        assert '测试工程师设计用例' in draft
                        assert 'scripts/TEST_INDEX.md' in draft
                        assert '产品质量结论单独标注' in draft
                    page.locator('#import-agents-reviewed').check()
                    page.locator('#import-next').click()
                    for index, role in enumerate(detail['roles']):
                        page.locator(f'#import-members input[data-bot-id="{fixture.ids[index]}"]').check()
                        page.locator('#import-members select').nth(index).select_option(role['name'])
                    page.locator('#import-next').click()
                    page.locator('#import-resource-step').wait_for(state='visible')
                    page.locator('#import-next').click()
                    page.locator('#import-review').wait_for(state='visible')
                    for role in detail['roles']:
                        assert role['name'] in page.locator('#import-review').inner_text()
                    page.locator('#import-submit').click()
                    page.locator('#dlg-project-import').wait_for(state='hidden')
                    if name == 'product-prototype':
                        assert (root / 'outputs/PRD.md').is_file()
                        assert (root / 'outputs/prototype/README.md').is_file()
                        assert (root / 'docs/design/UX.md').is_file()
                        assert (root / 'docs/validation/COVERAGE.md').is_file()
                        assert (root / 'materials/references/README.md').is_file()
                        assert '默认基线，可直接采用' in (root / 'ui-spec/UI-SPEC.md').read_text(encoding='utf-8')
                        assert 'ui-spec/UI-SPEC.md' in draft
                    elif name == 'algorithm':
                        assert (root / 'docs/literature/REVIEW.md').is_file()
                        assert '实验计划' in (root / 'docs/design/ALGORITHM.md').read_text(encoding='utf-8')
                        assert (root / 'reports/REPORT.md').is_file()
                        assert (root / 'reports/experiments/EXPERIMENT.md').is_file()
                        for unused in ('src', 'config', 'configs', 'docs/results', 'docs/experiments'):
                            assert not (root / unused).exists(), unused
                    else:
                        for path in ('docs/product/PRD.md', 'docs/product/PROTOTYPE.md', 'docs/cases/CASE.md',
                                     'docs/cases/COVERAGE.md', 'scripts/TEST_INDEX.md', 'reports/RUN.md', 'reports/DEFECT.md'):
                            assert (root / path).is_file(), path
                        assert not (root / 'tests').exists()
                    assert 'task node' in draft
                    assert not (root / 'prototype').exists()
                    assert not (root / 'docs/REVIEW.md').exists()
                    assert not (root / 'docs/ACCEPTANCE.md').exists()
                    assert not (root / 'internal').exists()
                    assert (root / 'scripts').exists() == (name == 'testing')
                    project = fixture.conn.execute('SELECT name FROM projects WHERE root=?', (str(root),)).fetchone()[0]
                    assert fixture.conn.execute('SELECT COUNT(*) FROM project_members WHERE project=?', (project,)).fetchone()[0] == len(detail['roles'])
                    page.reload()
                    page.evaluate('showLogin(false)')
                    page.evaluate('project => openProjectTemplateReferences(project)', project)
                    page.locator('#template-reference-content details').first.wait_for(state='visible')
                    assert page.locator('#template-reference-content details').count() == len(detail['roles'])
                    assert detail['roles'][0]['name'] in page.locator('#template-reference-content').inner_text()
                    page.locator('#dlg-template-reference').evaluate('d => d.close()')
                    continue
                page.locator('#import-close').click()
            # Failure is local to references; retry and version mismatch are explicit.
            page.evaluate('openProjectImport()')
            page.locator('#import-root').fill(str(fixture.root)); page.locator('#import-preview').click()
            page.locator('#import-config').wait_for(state='visible'); page.locator('#import-next').click()
            page.locator('#import-role-step').wait_for(state='visible')
            state['failure'] = True
            page.locator('#import-soul > summary').click()
            page.locator('#import-soul-retry').wait_for(state='visible')
            assert page.locator('#import-next').is_enabled()
            state['failure'] = False; state['stale'] = True
            page.locator('#import-soul-retry').click()
            page.wait_for_function("document.querySelector('#import-soul-status').textContent.includes('版本已变化')")
            state['stale'] = False
            page.locator('#import-soul-retry').click()
            page.locator('#import-soul-content details').first.wait_for(state='visible')
            page.locator('#import-close').click()
            # Legacy projects resolve the latest references by their template type.
            plan = fixture.preview().json()
            created = fixture.submit(plan).json()
            assert created['status'] == 'ready', created
            page.evaluate('project => openProjectTemplateReferences(project)', created['project'])
            refs = page.locator('#template-reference-content')
            refs.locator('details').first.wait_for(state='visible'); refs.locator('summary').first.click()
            latest = fixture.client.get('/projects/templates/research').json()
            assert refs.locator('pre').first.text_content() == latest['references'][0]['content']
            assert '最新参考' in page.locator('#dlg-template-reference h3').inner_text()
            assert not state['config_saves'], state['config_saves']
            fixture.assert_host_changes(before)
            assert not errors, errors
            browser.close()
        evidence = {'result': 'pass', 'cases': ['current new/import third step', 'six templates', 'copy/download', '390px',
                    'prototype four-role creation and reload', 'prototype docs and default UI spec', 'error retry', 'version mismatch',
                    'algorithm four-role creation and reload', 'algorithm user-managed code layout and reports',
                    'testing three-role creation and reload', 'testing PRD prototype case script mapping and reports',
                    'legacy project latest references', 'legacy UI removed'], 'bot_config_save_calls': state['config_saves']}
        if evidence_path: Path(evidence_path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print('PASS: current wizard references, templates, copy/download, retry/version guard, latest project references, preserved Bot files, 390px')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence'); parser.add_argument('--screenshot'); args = parser.parse_args()
    main(args.evidence, args.screenshot)
