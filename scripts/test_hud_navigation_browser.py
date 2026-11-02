"""Observe a real native assistant opening the actual project roles window."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(args.url.rstrip('/') + '/app/')
            try:
                page.wait_for_function("Boolean(window.ProjectHUD && typeof window.selectProject==='function')", timeout=10000)
            except Exception:
                (args.evidence / 'startup-errors.json').write_text(json.dumps({'errors': errors,
                    'url': page.url, 'body': page.locator('body').inner_text()[:1500]}, ensure_ascii=False, indent=2), encoding='utf-8')
                raise
            page.evaluate('project => selectProject(project)', args.project)
            page.wait_for_function("project => S.currentProject === project && document.querySelector('#hud-context').textContent.includes('项目维护')", arg=args.project)
            (args.evidence / 'ready.json').write_text('{"ready":true}', encoding='utf-8')
            page.wait_for_selector('#dlg-project-detail[open]', timeout=180000)
            page.wait_for_function("document.querySelector('#pj-tab-roles').getAttribute('aria-selected')==='true'")
            assert page.locator('#pj-detail-form').get_attribute('data-orig-name') == args.project
            assert page.locator('#pj-panel-roles').is_visible()
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000 if width == 1440 else 844})
                page.screenshot(path=str(args.evidence / f'opened-roles-{width}.png'))
            assert not errors, errors
            (args.evidence / 'result.json').write_text(json.dumps({'passed': True, 'project': args.project,
                'selected_tab': 'roles', 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
