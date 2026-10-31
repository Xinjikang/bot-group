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
                window.others=[];window.calls=[];window.failNext=false;
                window.api=async (path,options)=>{
                    window.calls.push({path,method:options?.method||'GET'});
                    window.lastAction={path,body:options?.body?JSON.parse(options.body):null};
                    if(window.failNext&&path.endsWith('/answer')){window.failNext=false;throw new Error('合成提交失败');}
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
                page.wait_for_function("document.querySelector('.hud-name').textContent==='测试助手'")

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
            assert '有待处理事项' not in page.locator('.hud-status').inner_text()
            assert page.locator('#hud-todos .hud-todo-card').count() == 0
            update({'question': {'id': 'q1', 'title': '可选整理？', 'reason': '普通建议', 'options': []}}, 'waiting')
            assert not opened()
            update({'proposal': {'id': 'p1', 'changes': {}, 'facts': [], 'reason': '可选建议'}}, 'waiting')
            assert not opened()
            update({'notification': {'id': 'n2', 'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
            assert opened()
            assert page.locator('#hud-chat-tab').get_attribute('aria-selected') == 'true'
            page.locator('#hud-todos-tab').click()
            assert page.locator('#hud-todos .hud-action-required').inner_text().startswith('请授权')
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
            assert '有待处理事项' not in page.locator('.hud-status').inner_text()
            assert page.locator('#hud-todos .hud-todo-card').count() == 0
            update({'question': {'id':'pending-q','title':'请确认资料位置','reason':'登记项目输入','options':[]}})
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
            page.evaluate("others=[{id:'other',project:'other-project',notification:{id:'other-n1',kind:'user_action',action:'其他项目操作'}},{id:'draft',title:'proj-7'},{id:'empty',title:''}];ProjectHUD.changed()")
            page.wait_for_timeout(200)
            assert not opened()
            assert page.evaluate("calls.every(c=>c.path!=='/hud'||c.method==='POST')"), 'Project HUD must never fetch the global session index'
            page.locator('#hud-toggle').click()
            page.locator('#hud-more').click()
            assert page.locator('#hud-tools button').all_text_contents() == ['项目备注', '设置']
            assert 'proj-7' not in page.locator('#project-hud').inner_text()
            assert '其他项目操作' not in page.locator('#project-hud').inner_text()
            page.locator('#hud-more').click()
            page.locator('#hud-chat-tab').click()
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
            assert page.locator('#hud-chat-tab').get_attribute('aria-selected') == 'true'
            assert page.locator('#hud-content .hud-todo-card').count() == 0
            page.locator('#hud-input').fill('对话草稿')
            page.locator('#hud-todos-tab').click()
            assert page.locator('#hud-content').is_hidden()
            assert page.locator('#hud-todos .hud-todo-card').count() == 15
            assert page.locator('#hud-todos-tab').inner_text() == '待办事项 (15)'
            page.locator('[data-card="q8"] textarea').fill('保留卡片输入')
            page.locator('#hud-chat-tab').click()
            assert page.locator('#hud-input').input_value() == '对话草稿'
            page.locator('#hud-chat-tab').press('ArrowRight')
            assert page.locator('[data-card="q8"] textarea').input_value() == '保留卡片输入'
            assert page.locator('#hud-todos-tab').evaluate('(e)=>e===document.activeElement')
            assert page.locator('.hud-question').count() == 9
            assert page.locator('.hud-proposal').count() == 6
            assert page.locator('.hud-message').count() == 2
            page.evaluate('failNext=true')
            page.locator('[data-card="q8"] .hud-primary').click()
            assert page.locator('[data-card="q8"] textarea').input_value() == '保留卡片输入'
            assert '合成提交失败' in page.locator('#hud-error').inner_text()
            page.locator('[data-card="q8"] .hud-option').click()
            assert page.evaluate('lastAction.body.question_id') == 'q8'
            assert page.locator('#hud-todos-tab').get_attribute('aria-selected') == 'true'
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
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':844})
                for theme in ('dark','light'):
                    page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
                    for tab in ('chat','todos'):
                        page.locator('#hud-'+tab+'-tab').click()
                        box=page.locator('#project-hud').bounding_box()
                        assert box['x']>=0 and box['x']+box['width']<=width+.5
                        assert page.locator('#hud-tabs').is_visible()
                        page.screenshot(path=str(args.evidence/f'tabs-{tab}-{width}-{theme}.png'))
            update({'question':{'id':'legacy-q','title':'单个问题','reason':'确认资料','options':[]},
                    'proposal':{'id':'legacy-p','changes':{},'facts':[{'label':'约定','value':'资料路径'}]}})
            assert page.locator('#hud-todos .hud-todo-card').count() == 2
            page.locator('[data-card="legacy-q"] textarea').fill('单项回答')
            page.locator('[data-card="legacy-q"] .hud-primary').click()
            assert page.evaluate('lastAction.body.question_id') == 'legacy-q'
            update({})
            assert page.locator('#hud-todos').inner_text() == '当前项目暂无待办事项。'
            assert page.locator('#hud-todos-tab').inner_text() == '待办事项'
            update({'cards':[{'proposal':{'id':'document-proposal','changes':{},'facts':[],
                'reason':'整理已提供的产品依据', 'documents':[
                    {'path':'docs/product/PRD.md','before':'# 原始需求\n版本待确认',
                     'content':'# 产品需求\n来源：用户提供的说明。\n'+'长内容测试 '*100,
                     'sources':['用户本轮提供的需求说明']},
                    {'path':'docs/product/PROTOTYPE.md','before':None,
                     'content':'# 原型\n页面和状态待用户确认。','sources':['用户提供的原型地址']}]}}]}, 'waiting')
            assert page.locator('.hud-document-preview').count() == 0
            assert 'docs/product/PRD.md' in page.locator('[data-card="document-proposal"]').inner_text()
            assert '长内容测试' not in page.locator('[data-card="document-proposal"]').inner_text()
            for width in (1440,390):
                page.set_viewport_size({'width':width,'height':844})
                for theme in ('dark','light'):
                    page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
                    box=page.locator('#project-hud').bounding_box()
                    assert box['x']>=0 and box['x']+box['width']<=width+.5
                    page.screenshot(path=str(args.evidence/f'document-summary-{width}-{theme}.png'))
            page.locator('[data-card="document-proposal"] .hud-primary').click()
            assert page.evaluate('lastAction.body.proposal_id') == 'document-proposal'
            assert page.locator('.hud-document-preview').count() == 0
            page.evaluate('''()=>{
                const form=document.createElement('form');form.id='pj-detail-form';form.dataset.origName='fixture';
                const input=document.createElement('textarea');input.id='pj-d-desc';input.value='原简介';
                form.append(input);document.body.append(form);
            }''')
            update({'config_update':{'id':'config-1','draft':False,'before':{'description':'原简介'},'changes':{'description':'助手已保存的简介'}}})
            assert page.locator('#pj-d-desc').input_value()=='助手已保存的简介'
            page.locator('#pj-d-desc').fill('用户正在编辑的草稿')
            update({'config_update':{'id':'config-2','draft':False,'before':{'description':'助手已保存的简介'},'changes':{'description':'第二次已保存的简介'}}})
            assert page.locator('#pj-d-desc').input_value()=='用户正在编辑的草稿'
            page.locator('#pj-detail-form').evaluate('(form)=>form.remove()')
            page.evaluate("fixture.id='second-session';fixture.project='second-project';S.currentProject='second-project';S.projects=[{name:'second-project',display_name:'second-project'}];fixture.entries=[];fixture.revision++;ProjectHUD.changed()")
            page.wait_for_function("document.querySelector('#hud-context').textContent.includes('second-project')")
            assert page.locator('#hud-chat-tab').get_attribute('aria-selected') == 'true'
            assert page.locator('#hud-input').input_value() == ''
            assert page.locator('#hud-todos .hud-todo-card').count() == 0
            assert not errors, errors
            (args.evidence/'notifications-browser.json').write_text(json.dumps({
                'passed': True, 'checks': ['member work never creates a badge or card; question/proposal stay collapsed',
                    'explicit action opens', 'collapse survives refresh', 'new action opens',
                    'preserve active reading', 'desktop/mobile', 'current project isolation and no global index requests', 'two tabs and per-card input', 'failed answer retained', 'legacy cards and empty state', 'scope switch resets tab',
                    'latest user/reply only', '12px user text', 'new send clears previous reply',
                    '15 cards all visible', 'answer ninth question preserves other cards',
                    'confirm sixth proposal preserves other cards', 'legacy documents show filenames without long text',
                    'native configuration updates synchronize untouched fields and preserve edited drafts',
                    '1440/390 dark/light document summary'],
                'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
