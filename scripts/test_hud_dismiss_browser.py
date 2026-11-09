"""Exercise immediate proposal dismissal with delayed and failed synthetic requests."""
import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <html><head><meta charset="utf-8"></head><body><div id="login" hidden></div><script>
                window.S={currentProject:'fixture',projects:[]};
                const proposal=id=>({id,changes:{description:'建议简介'},facts:[]});
                window.fixture={id:'session',project:'fixture',revision:1,status:'waiting',
                    bot:{display_name:'测试助手'},state:{proposal:proposal('p1'),
                        cards:[{proposal:proposal('p2')}],notification:{id:'p1',kind:'user_action',action:'请确认简介'}},entries:[]};
                window.calls=[];window.fail=false;window.failRead=false;window.loadProjects=async()=>{};
                window.api=async(path,options)=>{
                    if(path.endsWith('/answer')){
                        calls.push(JSON.parse(options.body));
                        await new Promise(resolve=>window.release=resolve);
                        if(fail)throw new Error('合成提交失败');
                        const id=JSON.parse(options.body).dismiss_proposal;
                        if(fixture.state.proposal?.id===id)delete fixture.state.proposal;
                        fixture.state.cards=fixture.state.cards.filter(c=>c.proposal?.id!==id);
                        fixture.state.notification={kind:'none'};
                        fixture.status='queued';fixture.revision++;
                    }else if(failRead&&path==='/hud/session')throw new Error('合成读取失败');
                    return structuredClone(fixture);
                };
                </script></body></html>'''))
            for width in (1440, 390):
                for failure in (False, True):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.goto('http://hud-dismiss.test/')
                    for style in re.findall(r'<style[^>]*>(.*?)</style>', (root/'frontend/index.html').read_text(encoding='utf-8'), re.S):
                        page.add_style_tag(content=style)
                    page.add_style_tag(path=str(root/'frontend/appearance.css'))
                    page.add_style_tag(path=str(root/'frontend/project_hud.css'))
                    page.add_script_tag(path=str(root/'frontend/project_hud.js'))
                    page.wait_for_function("document.querySelector('.hud-name').textContent==='测试助手'")
                    page.locator('#hud-todos-tab').click()
                    page.locator('#hud-input').fill('保留对话草稿')
                    page.evaluate('(failure)=>{fail=failure;failRead=failure}', failure)
                    page.locator('[data-card="p1"]').get_by_role('button', name='暂不处理', exact=True).click()
                    assert page.locator('[data-card="p1"]').count() == 0
                    assert page.locator('[data-card="p2"]').count() == 1
                    assert page.locator('#hud-todos .hud-reminder').count() == 0
                    assert page.locator('#hud-hint').inner_text() == '正在发送…'
                    assert page.evaluate('calls[0].dismiss_proposal') == 'p1'
                    assert '请继续当前问题' in page.evaluate('calls[0].text')
                    page.screenshot(path=str(args.evidence/f'pending-{width}-{failure}.png'))
                    page.evaluate('release()')
                    page.wait_for_function("document.querySelector('#hud-hint').textContent!=='正在发送…'")
                    assert page.locator('#hud-input').input_value() == '保留对话草稿'
                    assert page.locator('#hud-todos-tab').get_attribute('aria-selected') == 'true'
                    if failure:
                        assert page.locator('[data-card="p1"]').count() == 1
                        assert '合成提交失败' in page.locator('#hud-error').inner_text()
                        assert page.locator('[data-card="p1"] button').first.is_enabled()
                    else:
                        assert page.locator('[data-card="p1"]').count() == 0
                        assert page.evaluate('fixture.status') == 'queued'
                        page.evaluate("fixture.status='idle';fixture.revision++;ProjectHUD.changed()")
                        page.wait_for_function("!document.querySelector('[data-card=p2] button').disabled")
                        page.locator('[data-card="p2"]').get_by_role('button', name='暂不处理', exact=True).click()
                        assert page.locator('.hud-proposal').count() == 0
                        assert '当前项目暂无待办事项' in page.locator('#hud-todos').inner_text()
                        page.evaluate('release()')
                        page.wait_for_function("document.querySelector('#hud-hint').textContent!=='正在发送…'")
                    page.screenshot(path=str(args.evidence/f'result-{width}-{failure}.png'))
            assert not errors, errors
            (args.evidence/'dismiss-browser.json').write_text(json.dumps({
                'passed': True, 'widths': [1440, 390], 'checks': [
                    'card disappears before request resolves', 'other card retained', 'target reminder removed',
                    'assistant continuation submitted', 'success preserves closed card through model wait',
                    'failed submission and recovery read restore card and error', 'last card empty state',
                    'draft and active tab retained', 'sending hint clears'], 'errors': errors},
                ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
