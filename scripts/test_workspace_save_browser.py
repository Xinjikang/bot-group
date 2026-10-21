"""Workspace save keeps the resource configuration entry available."""
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    source = (Path(__file__).resolve().parents[1] / 'frontend/project_settings.js').read_text(encoding='utf-8')
    render = source[source.index('function renderWorkspaceMode()'):source.index("document.querySelectorAll('[name=\"member-directory-mode\"]')", source.index('function renderWorkspaceMode()'))]
    save = source[source.index('async function saveWorkspace()'):]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.set_content('<div id="pj-detail-form" data-orig-name="p"></div><dialog id="dlg-workspace"><div id="workspace-current"></div><button id="workspace-resources">资源配置</button><button id="workspace-remove"></button><div id="workspace-options"></div><button id="workspace-save-mode"></button><form id="workspace-form"><input id="workspace-path" value="E:/test/member"></form><div id="workspace-result"></div></dialog>')
        page.add_script_tag(content="const $ = s => document.querySelector(s); let workspaceEditor; const projectMembersCache = new Map(); const loadProjectMembers = async () => {}; const api = async () => ({path:'E:/test/member'});" + render + save)
        for action in ('create', 'adopt'):
            page.evaluate('''action => {
              workspaceEditor = {project:'p',profile:'dev',action,member:{workspace_assigned:false},mode:'worktree',savedMode:'worktree'};
              $('#dlg-workspace').showModal(); renderWorkspaceMode(); workspaceEditor.action=action;
            }''', action)
            assert page.locator('#workspace-resources').is_hidden()
            page.evaluate('saveWorkspace()')
            assert page.locator('#dlg-workspace').evaluate('el => el.open')
            assert page.locator('#workspace-resources').is_visible()
            assert page.locator('#workspace-resources').is_enabled()
            assert page.locator('#workspace-options').is_hidden()
            assert page.locator('#workspace-form').is_hidden()
            assert 'E:/test/member' in page.locator('#workspace-current').inner_text()
            assert '可继续查看和调整资源配置' in page.locator('#workspace-result').inner_text()
            page.evaluate("$('#dlg-workspace').close()")
        browser.close()
    print('PASS: create/adopt retain window, show enabled resources, refresh path and hide creation form')


if __name__ == '__main__':
    main()
