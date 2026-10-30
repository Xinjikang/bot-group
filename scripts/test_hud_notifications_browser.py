"""Exercise HUD attention using synthetic API data and the repository UI assets."""
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
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <html><head><meta charset="utf-8"></head><body><div id="login" hidden></div><script>
                window.S={currentProject:'fixture',projects:[]};
                window.fixture={id:'session',project:'fixture',revision:1,status:'idle',
                    bot:{display_name:'测试助手'},state:{notification:{id:'n1',kind:'pending'}},
                    entries:[{id:1,role:'assistant',content:{text:'测试脚本由执行成员收尾。'}}]};
                window.others=[];
                window.api=async (path,options)=>{
                    window.lastAction={path,body:options?.body?JSON.parse(options.body):null};
                    if(path.endsWith('/answer')){
                        fixture.entries.push({id:fixture.entries.at(-1).id+1,role:'user',content:{text:JSON.parse(options.body).text}});
                        const questionId=JSON.parse(options.body).question_id;
                        fixture.status='queued';fixture.state={cards:(fixture.state.cards||[]).filter(c=>c.question?.id!==questionId)};fixture.revision++;
                    }
                    if(path.endsWith('/accept')){
                        const id=JSON.parse(options.body).proposal_id;
                        fixture.state.cards=fixture.state.cards.filter(c=>c.proposal?.id!==id);fixture.revision++;
                    }
                    return structuredClone(path==='/hud'&&options?.method!=='POST'?{sessions:others}:fixture);
                };
                </script></body></html>'''))
            def load():
                page.goto('http://hud-fixture.test/')
                styles = re.findall(r'<style[^>]*>(.*?)</style>', (root/'frontend/index.html').read_text(encoding='utf-8'), re.S)
                for style in styles:
                    page.add_style_tag(content=style)
                page.add_style_tag(path=str(root/'frontend/appearance.css'))
                page.add_style_tag(path=str(root/'frontend/project_hud.css'))
                page.add_script_tag(path=str(root/'frontend/project_hud.js'))
                page.wait_for_function("document.querySelector('.hud-status').textContent.includes('有待处理事项')")

            def update(state, status='idle'):
                page.evaluate('''({state,status})=>{
                    fixture.state=state;fixture.status=status;fixture.revision++;
                    ProjectHUD.changed();
                }''', {'state': state, 'status': status})
                page.wait_for_timeout(150)

            def opened():
                return page.locator('#hud-toggle').get_attribute('aria-expanded') == 'true'

            load()
            assert not opened()
            update({'question': {'id': 'q1', 'title': '可选整理？', 'reason': '普通建议', 'options': []}}, 'waiting')
            assert not opened()
            update({'proposal': {'id': 'p1', 'changes': {}, 'facts': [], 'reason': '可选建议'}}, 'waiting')
            assert not opened()
            update({'notification': {'id': 'n2', 'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
            assert opened()
            assert page.locator('.hud-action-required').inner_text().startswith('请授权')
            page.locator('#hud-toggle').click()
            update({'notification': {'id': 'n2', 'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
            assert not opened()
            load()
            update({'notification': {'id': 'n2', 'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
            assert not opened(), 'Collapsed reminder must stay quiet after reload'
            update({'notification': {'id': 'n3', 'kind': 'user_action', 'action': '请确认新的交付范围。'}})
            assert opened()
            update({'notification': {'id': 'n4', 'kind': 'pending'}})
            assert opened(), 'Preserve a panel already being read'
            page.locator('#hud-toggle').click()
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 844})
                assert not opened()
                assert '有待处理事项' in page.locator('.hud-status').inner_text()
                for theme in ('dark', 'light'):
                    page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
                    page.screenshot(path=str(args.evidence/f'pending-{width}-{theme}.png'))
            update({'notification': {'id': 'n5', 'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
            assert opened()
            page.screenshot(path=str(args.evidence/'action-390.png'))
            page.locator('#hud-toggle').click()
            page.evaluate("others=[{id:'other',project:'other-project',notification:{id:'other-n1',kind:'pending'}}];ProjectHUD.changed()")
            page.wait_for_function("document.querySelector('.hud-status').textContent.includes('另有 1 项待处理')")
            assert not opened()
            page.evaluate("others[0].notification={id:'other-n2',kind:'user_action',action:'请提供项目资料'};ProjectHUD.changed()")
            page.wait_for_selector('#hud-notices', state='visible')
            assert opened()
            assert '需要你行动' in page.locator('#hud-notices').inner_text()
            page.locator('#hud-back').click()
            page.evaluate('''()=>{
                fixture.entries=[{id:1,role:'user',content:{text:'旧问题'}},
                    {id:2,role:'assistant',content:{text:'旧回答'}},
                    {id:3,role:'user',content:{text:'当前问题'}},
                    {id:4,role:'assistant',content:{text:'当前回答'}},
                    {id:5,role:'system',content:{text:'保存记录'}}];
                fixture.state={};fixture.revision++;ProjectHUD.changed();
            }''')
            page.wait_for_function("document.querySelector('.hud-message-user')?.textContent.includes('当前问题')")
            assert page.locator('.hud-message').count() == 2
            assert '旧问题' not in page.locator('#hud-content').inner_text()
            assert page.locator('.hud-message-user .hud-message-text').evaluate('(e)=>getComputedStyle(e).fontSize') == '12px'
            page.screenshot(path=str(args.evidence/'latest-turn-390.png'))
            page.locator('#hud-input').fill('新问题')
            page.locator('#hud-send').click()
            page.wait_for_function("document.querySelector('.hud-message-user')?.textContent.includes('新问题')")
            assert page.locator('.hud-message').count() == 1
            assert page.locator('.hud-message-assistant').count() == 0
            page.evaluate('''()=>{
                fixture.entries.push({id:7,role:'assistant',content:{text:'新回答'}});
                fixture.status='idle';fixture.revision++;ProjectHUD.changed();
            }''')
            page.wait_for_function("document.querySelector('.hud-message-assistant')?.textContent.includes('新回答')")
            assert page.locator('.hud-message').count() == 2
            questions = [{'text': f'待办说明 {i}', 'question': {'id': f'q{i}', 'title': f'待办问题 {i}',
                         'reason': '登记输入', 'options': ['已准备']}} for i in range(9)]
            proposals = [{'text': f'备注说明 {i}', 'proposal': {'id': f'p{i}', 'changes': {},
                          'facts': [{'label': f'资料 {i}', 'value': '登记位置'}]}} for i in range(6)]
            update({'cards': questions + proposals}, 'waiting')
            assert page.locator('.hud-question').count() == 9
            assert page.locator('.hud-proposal').count() == 6
            assert page.locator('.hud-message').count() == 2
            page.locator('[data-card="q8"] .hud-option').click()
            assert page.evaluate('lastAction.body.question_id') == 'q8'
            assert page.locator('.hud-question').count() == 8
            assert page.locator('.hud-proposal').count() == 6
            page.evaluate("fixture.status='waiting';fixture.revision++;ProjectHUD.changed()")
            page.wait_for_timeout(150)
            page.locator('[data-card="p5"] .hud-primary').click()
            assert page.evaluate('lastAction.body.proposal_id') == 'p5'
            assert page.locator('.hud-question').count() == 8
            assert page.locator('.hud-proposal').count() == 5
            page.locator('.hud-scroll').evaluate('(element)=>element.scrollTop=0')
            page.screenshot(path=str(args.evidence/'many-cards-390.png'))
            assert not errors, errors
            (args.evidence/'notifications-browser.json').write_text(json.dumps({
                'passed': True, 'checks': ['pending text/question/proposal stay collapsed',
                    'explicit action opens', 'collapse survives refresh', 'new action opens',
                    'preserve active reading', 'desktop/mobile', 'other project notification gate',
                    'latest user/reply only', '12px user text', 'new send clears previous reply',
                    '15 cards all visible', 'answer ninth question preserves other cards',
                    'confirm sixth proposal preserves other cards'],
                'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
