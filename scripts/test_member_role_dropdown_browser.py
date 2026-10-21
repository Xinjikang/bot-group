"""Isolated Chromium regression for the member role dropdown."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / 'frontend/project_settings.js').read_text(encoding='utf-8')
    function = source[source.index('function renderMemberRoleMenu('):source.index('function roleStatus(')]
    html = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    css = html[html.index('.project-member-role-menu{'):html.index('.pj-role-row{')]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 600})
        page.set_content('<style>' + css + '</style><dialog style="width:330px"><div id="clip" style="height:220px;overflow:auto"><div id="row" style="margin-top:160px"><button id="role">设置角色</button><span id="status"></span></div></div></dialog>')
        page.evaluate("document.querySelector('dialog').showModal()")
        page.add_script_tag(content="const projectRolesCache = new Map(); const api = async (url, options) => options ? {role:'开发',role_id:1} : {roles:Array.from({length:12}, (_,i)=>({id:i+1,name:i?'角色'+i:'开发'}))}; const loadMembers = async () => {};" + function)
        page.evaluate("role.onclick = () => renderMemberRoleMenu(document.querySelector('#row'), 'p', {}, role, document.querySelector('#status'))")
        before = page.locator('#row').bounding_box()['height']
        page.locator('#role').click()
        menu = page.locator('.project-member-role-menu')
        menu.locator('button').first.wait_for()
        assert menu.evaluate("el => el.matches(':popover-open')")
        assert page.locator('#row').bounding_box()['height'] == before
        rect = menu.bounding_box()
        assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 390
        assert rect['y'] >= 0 and rect['y'] + rect['height'] <= 600
        menu.locator('button').first.click()
        assert page.locator('#role').inner_text() == '开发'
        page.locator('#role').click()
        menu.wait_for()
        page.keyboard.press('Escape')
        menu.wait_for(state='detached')
        assert page.locator('dialog').evaluate('el => el.open')
        page.locator('#role').click()
        menu.wait_for()
        page.locator('dialog').click(position={'x': 5, 'y': 5})
        menu.wait_for(state='detached')
        browser.close()
    print('PASS: top layer, unchanged layout, viewport bounds, selection, Escape and outside dismissal')


if __name__ == '__main__':
    main()
