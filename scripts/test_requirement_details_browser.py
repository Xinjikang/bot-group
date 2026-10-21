"""Real browser, synthetic requirement data and local assets only."""
import re
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    html = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
    css = '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', html, re.S))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<style>' + css + '</style><div id="task-list"></div>')
        page.evaluate('''() => {
          window.S = {currentProject:'p',isUserIdentity:true,members:[
            {profile:'bot-37',host_profile:'dev-01',display_name:'江澈',runtime:'codex'},
            {profile:'reviewer-01',host_profile:'reviewer-01',display_name:'周岚',runtime:'hermes'}]};
          window.$ = s => document.querySelector(s);
          window.esc = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;');
          window.NAME_OF = s => s;
          window.openTaskHistory = () => {};
          window.api = async () => ({requirement:{id:1,project:'p',title:'需求详情验证',
            version:'v2',status:'open',description:'完整正文\\n'+'验收标准与设计依据。'.repeat(600)},
            tasks:[{id:2,title:'接口实现',status:'pending',briefing_status:'ready',next_bot:'bot-37',
              prerequisites:'[1]',dependency_blockers:[{id:1,title:'基础结构',status:'blocked'}]}]});
        }''')
        start = html.index('const STALE_MS =')
        # The renderTasks block ends before the next top-level API handlers.
        render_end = html.index('\nasync function ', html.index('function renderTasks', start))
        page.add_script_tag(path=str(ROOT / 'frontend/member_identity.js'))
        page.add_script_tag(content=html[start:render_end])
        page.add_script_tag(path=str(ROOT / 'frontend/requirement_details.js'))
        page.locator('#task-list').evaluate("e => e.style.width='290px'")
        page.evaluate("renderTasks([{id:1,project:'p',title:'Codex driver spawn and session mapping',status:'done'}],[{id:2,requirement_id:1,status:'done',claimed_by:'reviewer-01',next_bot:'bot-37'}],[])")
        assert page.locator('.t-next').text_content() == '→江澈'
        assert page.locator('.t-next').get_attribute('title') == '江澈（dev-01）'
        assert page.locator('.t-claimed').text_content() == '周岚'
        group = page.locator('.req-actions')
        page.locator('details').evaluate_all('items => items.forEach(e => e.open=true)')
        assert group.locator('button').count() == 2
        assert group.evaluate("e => getComputedStyle(e).opacity") == '0'
        page.locator('.req-title').hover()
        page.wait_for_function("getComputedStyle(document.querySelector('.req-actions')).opacity === '1'")
        buttons = group.locator('button').all()
        assert abs(buttons[0].bounding_box()['y'] - buttons[1].bounding_box()['y']) < 1
        assert buttons[0].evaluate("e => getComputedStyle(e).whiteSpace") == 'nowrap'
        assert buttons[0].evaluate("e => getComputedStyle(e).backgroundColor") == 'rgba(0, 0, 0, 0)'
        page.screenshot(path=str(Path(tempfile.gettempdir()) / 'bot-group-requirement-actions.png'))
        page.evaluate("renderTasks([{id:1,project:'p',title:'需求详情验证',status:'open'}],[],[])")
        page.locator('.req-title').hover()
        page.get_by_role('button', name='查看', exact=True).click()
        page.get_by_text('等待：#1 基础结构（已阻塞）', exact=True).wait_for()
        assert len(page.locator('[data-content]').inner_text()) > 6000
        assert '江澈（dev-01）' in page.locator('[data-content]').inner_text()
        assert 'bot-37' not in page.locator('[data-content]').inner_text()
        page.set_viewport_size({'width': 390, 'height': 844})
        rect = page.locator('#dlg-requirement-details').bounding_box()
        assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 390
        assert page.locator('#dlg-requirement-details').evaluate('(e) => e.scrollWidth <= e.clientWidth')
        screenshot = Path(tempfile.gettempdir()) / 'bot-group-requirement-details.png'
        page.screenshot(path=str(screenshot))
        assert page.locator('[data-requirement-field]').count() == 7
        assert page.get_by_text('历史需求描述', exact=True).is_visible()
        page.evaluate('''() => { window.api = async () => ({requirement:{id:1,project:'p',
            title:'七字段',background_goal:'背景与目标正文',scope:'范围正文',approach_basis:'方案依据正文',
            acceptance:'验收正文',undecided:'无',source_ref:'thread_id=10082; message_id=5600 <img src=x onerror=alert(1)>'},tasks:[]}); }''')
        page.get_by_role('button', name='刷新', exact=True).click()
        page.get_by_text('背景与目标正文', exact=True).wait_for()
        assert page.locator('[data-requirement-field]').count() == 7
        assert 'thread_id=10082; message_id=5600' in page.locator('[data-requirement-field="source_ref"]').inner_text()
        assert page.locator('[data-content] img').count() == 0
        assert page.locator('#dlg-requirement-details').evaluate('(e) => e.scrollWidth <= e.clientWidth')
        page.evaluate('''() => { window.api = async () => ({requirement:{id:1,project:'p',title:'空字段',
            background_goal:null,scope:' ',source_ref:''},tasks:[]}); }''')
        page.get_by_role('button', name='刷新', exact=True).click()
        page.get_by_text('暂未填写来源关联', exact=True).wait_for()
        assert page.get_by_text('暂未填写背景与目标', exact=True).is_visible()
        page.get_by_role('button', name='关闭', exact=True).click()
        assert not page.locator('#dlg-requirement-details').is_visible()
        # Human recovery stays available and submits the requirement's project.
        page.add_script_tag(content=html[html.index('async function completeRequirement('):html.index('async function resumeRequirement(')])
        page.evaluate('''() => {
          window.closeCalls=[]; window.closeReloads=0; window.acceptClose=false;
          window.failClose=false; window.closeError='';
          window.showConfirm=async () => acceptClose;
          window.showAlert=async message => { closeError=message; };
          window.api=async (path, options) => {
            closeCalls.push([path,JSON.parse(options.body)]);
            if(failClose) throw new Error('unfinished tasks');
            return {completed:true};
          };
          window.loadTasks=async () => { closeReloads++; };
          S.currentProject=null;
          renderTasks([{id:8,project:'q',title:'待关闭需求',status:'open'}], [
            {id:81,project:'q',requirement_id:8,title:'未完成',status:'pending'},
            {id:82,project:'q',requirement_id:8,title:'已完成',status:'done'}],[]);
        }''')
        page.locator('details').evaluate_all('items => items.forEach(e => e.open=true)')
        assert page.locator('.req-complete-btn').count() == 2
        for index in (0, 1):
            page.locator('.req-title').nth(index).hover()
            page.locator('.req-complete-btn').nth(index).click()
        assert page.evaluate('closeCalls.length') == 0
        page.evaluate('acceptClose=true; failClose=true')
        page.locator('.req-complete-btn').last.click()
        page.wait_for_function("closeError.includes('unfinished tasks')")
        assert page.evaluate('closeReloads') == 0
        page.evaluate('failClose=false')
        page.locator('.req-complete-btn').last.click()
        page.wait_for_function('closeReloads === 1')
        assert page.evaluate('closeCalls[1]') == ['/admin/requirement/8/complete', {'project': 'q'}]
        page.evaluate("renderTasks([{id:8,project:'q',title:'已关闭',status:'done'}],[{id:82,requirement_id:8,status:'done'}],[])")
        assert page.locator('.req-complete-btn').count() == 0
        assert page.locator('.req-archive-btn').count() == 1
        # A requirement with no tasks keeps its history and archive controls after closing.
        archive_start = html.index('async function archiveRequirement(')
        page.add_script_tag(content=html[archive_start:html.index('async function adminTaskAction(', archive_start)])
        page.evaluate('''() => {
          window.emptyRequirement={id:83,project:'q',title:'零任务需求',status:'open',archived:false};
          window.emptyCalls=[];
          window.showConfirm=async () => true;
          window.api=async (path, options) => {
            emptyCalls.push([path,JSON.parse(options.body)]);
            if(path.endsWith('/complete')) emptyRequirement.status='done';
            if(path.endsWith('/archive')) emptyRequirement.archived=true;
            if(path.endsWith('/unarchive')) emptyRequirement.archived=false;
            return {};
          };
          window.loadTasks=async () => renderTasks(
            emptyRequirement.archived && !S.showArchived ? [] : [emptyRequirement],[],[]);
          S.showArchived=false; loadTasks();
        }''')
        assert page.locator('.req-block').count() == 1
        page.locator('.req-title').hover()
        page.locator('.req-complete-btn').click()
        page.wait_for_function("document.querySelector('.req-archive-btn') !== null")
        assert page.locator('.req-block').count() == 1
        assert page.locator('.task-history .req-block').count() == 1
        page.locator('.task-history').evaluate('e => e.open=true')
        assert page.locator('.req-task-count').text_content() == '历史 0'
        assert page.locator('.req-complete-btn').count() == 0
        page.locator('.req-title').hover()
        page.locator('.req-archive-btn').click()
        page.wait_for_function("document.querySelectorAll('.req-block').length === 0")
        page.locator('.task-history').evaluate('e => e.open=true')
        page.locator('#show-archived').check()
        page.get_by_role('button', name='取消归档', exact=True).wait_for(state='attached')
        page.locator('.task-history').evaluate('e => e.open=true')
        page.locator('.req-title').hover()
        page.get_by_role('button', name='取消归档', exact=True).click()
        page.wait_for_function("document.querySelector('.req-archive-btn')?.textContent === '归档'")
        assert page.evaluate('emptyCalls') == [
            ['/admin/requirement/83/complete', {'project': 'q'}],
            ['/admin/requirement/83/archive', {'project': 'q'}],
            ['/admin/requirement/83/unarchive', {'project': 'q'}]]
        # Withdrawn requirements without tasks do not become empty history cards.
        for is_user in (True, False):
            page.evaluate('''isUser => {
              S.isUserIdentity=isUser;
              renderTasks([
                {id:97,project:'q',title:'重复拆解根因核查',status:'cancelled'},
                {id:85,project:'q',title:'steer运行中纠偏落地',status:'cancelled'},
                {id:84,project:'q',title:'stop command unified management',status:'cancelled',archived:true},
                {id:83,project:'q',title:'已完成的零任务需求',status:'done'},
                {id:90,project:'q',title:'已完成需求',status:'done'},
                {id:91,project:'q',title:'有历史任务的撤回需求',status:'cancelled'}], [
                {id:155,requirement_id:90,status:'done'},
                {id:156,requirement_id:91,status:'cancelled'}]);
            }''', is_user)
            assert page.locator('.req-block').count() == 3
            for title in ('重复拆解根因核查', 'steer运行中纠偏落地', 'stop command unified management'):
                assert page.get_by_text(title, exact=False).count() == 0
            assert page.locator('.task-row').count() == 2
            assert page.locator('.req-archive-btn').count() == (3 if is_user else 0)
        page.evaluate('S.isUserIdentity=true')
        page.add_script_tag(content=html[html.index('async function resumeRequirement('):html.index('async function archiveRequirement(')])
        page.evaluate('''() => {
          window.resumeCalls=[]; window.reloads=0; window.acceptResume=false;
          window.showConfirm=async () => window.acceptResume;
          window.showAlert=async message => { window.resumeError=message; };
          window.api=async (path, options) => { window.resumeCalls.push([path,JSON.parse(options.body)]); return {resumed:true}; };
          window.loadTasks=async () => { window.reloads++; };
          window.drawResume=() => renderTasks([{id:9,project:'q',title:'阻塞需求',status:'blocked'}],
            [{id:10,project:'q',requirement_id:9,title:'阻塞任务',status:'blocked'}],[]);
          S.currentProject=null; drawResume();
        }''')
        assert page.locator('.tad-resume').count() == 1
        page.locator('.req-title').hover()
        page.wait_for_function("getComputedStyle(document.querySelector('.req-actions')).opacity === '1'")
        page.locator('.req-resume-btn').click()
        assert page.evaluate('resumeCalls.length') == 0
        page.evaluate('acceptResume=true')
        page.locator('.req-resume-btn').click()
        page.wait_for_function('reloads === 1')
        assert page.evaluate('resumeCalls') == [['/admin/requirement/9/resume', {'project': 'q'}]]
        page.evaluate('S.isUserIdentity=false; drawResume()')
        assert page.locator('.req-resume-btn').count() == 0
        assert page.locator('.req-complete-btn').count() == 0
        assert page.locator('.tad-resume').count() == 0
        # Selected topic chips show public labels while retaining internal IDs.
        page.evaluate('''() => {
          document.body.insertAdjacentHTML('beforeend', '<div id="ms-chips"></div><span id="ms-placeholder"></span><div id="ms-options"></div>');
          window.selectedMembers=new Set(['bot-37','reviewer-01','legacy']);
        }''')
        page.add_script_tag(content=html[html.index('function renderChips()'):html.index('$("#ms-trigger").addEventListener')])
        page.evaluate('renderChips()')
        assert page.locator('.ms-chip').all_text_contents() == ['江澈（dev-01） ×', '周岚（reviewer-01） ×', 'legacy ×']
        assert page.locator('.ms-chip .x').evaluate_all('(items) => items.map(e=>e.dataset.p)') == ['bot-37','reviewer-01','legacy']
        page.locator('.ms-chip .x[data-p="bot-37"]').click()
        assert page.evaluate('Array.from(selectedMembers)') == ['reviewer-01', 'legacy']
        assert not errors, errors
        browser.close()
        print('PASS: real View button, full text, dependencies, narrow layout and close; screenshot:', screenshot)


if __name__ == '__main__':
    main()
