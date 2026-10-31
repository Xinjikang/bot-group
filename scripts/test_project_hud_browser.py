"""Real-browser HUD checks against preview_project_hud.py's isolated fixture."""
import argparse
import json
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    args.evidence.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        assert page.request.post(args.url+'/hud-preview/reset').ok
        page.goto(args.url+'/app/')
        page.wait_for_selector('#hud-tabs')
        assert page.locator('#hud-chat-tab').get_attribute('aria-selected')=='true'
        assert '先把需求资料准备好' in page.locator('.hud-message-assistant').last.inner_text()
        page.locator('#hud-todos-tab').click()
        page.wait_for_selector('.hud-question')
        page.screenshot(path=str(args.evidence/'hud-desktop.png'))
        page.locator('#hud-toggle').click()
        page.evaluate("openProjectDetail('hud-preview',document.querySelector('#chip-all'))")
        page.wait_for_selector('#dlg-project-detail[open]')
        page.locator('#pj-d-desc').fill('尚未保存的项目配置')
        page.locator('#pj-tab-roles').click()
        original_detail=page.locator('#dlg-project-detail').bounding_box()
        for tab in ('agents','members','permissions','basic','roles'):
            page.locator('#pj-tab-'+tab).click()
            page.wait_for_timeout(100)
            assert page.locator('#dlg-project-detail').bounding_box()==original_detail,tab
        original_hud=page.locator('#project-hud').bounding_box()
        page.locator('#hud-toggle').click()
        assert page.locator('#dlg-project-detail').evaluate('(e)=>e.open')
        assert page.locator('#pj-tab-roles').get_attribute('aria-selected')=='true'
        page.locator('#hud-input').fill('一起完善这个项目')
        assert page.locator('.hud-option').count()==3
        assert page.locator('#dlg-project-detail').evaluate('(e)=>e.open')
        assert page.locator('#pj-d-desc').input_value()=='尚未保存的项目配置'
        detail_box=page.locator('#dlg-project-detail').bounding_box()
        hud_box=page.locator('#project-hud').bounding_box()
        assert detail_box==original_detail,(detail_box,original_detail)
        assert abs(detail_box['x']+detail_box['width']+8-hud_box['x'])<1
        assert abs(detail_box['y']-hud_box['y'])<1
        assert abs(detail_box['height']-hud_box['height'])<1
        assert page.locator('.hud-scroll').bounding_box()['height']>=80
        page.screenshot(path=str(args.evidence/'hud-with-project-detail.png'))
        for width in (1120,1040):
            page.set_viewport_size({'width':width,'height':1000})
            page.wait_for_timeout(100)
            detail_box=page.locator('#dlg-project-detail').bounding_box()
            hud_box=page.locator('#project-hud').bounding_box()
            assert detail_box['x']>=0 and hud_box['x']+hud_box['width']<=width
            if width-detail_box['x']-detail_box['width']-16>=300:
                assert detail_box['x']+detail_box['width']<hud_box['x']
                assert abs(detail_box['height']-hud_box['height'])<1
            else:
                page.locator('#hud-toggle').click()
                assert page.locator('#dlg-project-detail').bounding_box()==detail_box
                page.locator('#hud-toggle').click()
        page.set_viewport_size({'width':1440,'height':1000})
        page.locator('#hud-input').fill('')
        page.locator('#hud-toggle').click()
        assert page.locator('#dlg-project-detail').bounding_box()==original_detail
        assert page.locator('#project-hud').bounding_box()==original_hud
        page.locator('#pj-tab-basic').click()
        page.locator('#hud-toggle').click()
        page.wait_for_timeout(100)
        resized_detail=page.locator('#dlg-project-detail').bounding_box()
        assert abs(resized_detail['height']-page.locator('#project-hud').bounding_box()['height'])<1
        page.locator('#hud-toggle').click()
        assert page.locator('#pj-d-desc').input_value()=='尚未保存的项目配置'
        page.locator('#dlg-pj-detail-close').click()
        page.locator('#hud-toggle').click()
        page.locator('#hud-input').fill('需求放在 specs-v2，原型放在 design-sketches。')
        page.reload()
        page.locator('#hud-toggle').click()
        page.wait_for_selector('#hud-input')
        page.wait_for_function("document.querySelector('#hud-input').value.includes('specs-v2')")
        assert 'specs-v2' in page.locator('#hud-input').input_value()
        page.locator('#hud-todos-tab').click()
        page.locator('.hud-card-answer').fill(page.locator('#hud-input').input_value())
        page.locator('#hud-input').fill('')
        page.locator('.hud-option').first.click()
        page.wait_for_selector('.hud-proposal',timeout=20000)
        assert page.locator('.hud-option').count()==0
        assert page.locator('#project-hud').get_attribute('data-scene')=='cards'
        page.screenshot(path=str(args.evidence/'hud-confirmation.png'))
        page.locator('#hud-chat-tab').click()
        assert '已有完整文档' in page.locator('.hud-message-user').last.inner_text()
        assert 'specs-v2' in page.locator('.hud-message-user').last.inner_text()
        assert '项目简介中登记本次资料说明' in page.locator('.hud-message-assistant').last.inner_text()
        assert page.locator('#hud-input').input_value()==''
        for selector in ('.hud-message-user .hud-message-text','.hud-message-assistant .hud-message-text'):
            assert page.locator(selector).last.evaluate("e=>{const a=e.getBoundingClientRect(),b=document.querySelector('.hud-scroll').getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom}")
        page.screenshot(path=str(args.evidence/'hud-conversation.png'))
        page.reload()
        page.locator('#hud-toggle').click()
        page.wait_for_selector('.hud-proposal',state='attached')
        page.locator('#hud-todos-tab').click()
        page.wait_for_selector('.hud-proposal')
        assert page.locator('.hud-option').count()==0
        assert '以下建议尚未保存' in page.locator('.hud-proposal').inner_text()
        assert '当前内容' in page.locator('.hud-proposal').inner_text()
        assert '建议改为' in page.locator('.hud-proposal').inner_text()
        assert '修改位置：项目设置' in page.locator('.hud-proposal').inner_text()
        page.get_by_role('button',name='确认修改项目设置',exact=True).click()
        page.wait_for_function("!document.querySelector('.hud-proposal')",timeout=20000)
        projects=page.request.get(args.url.rstrip('/')+'/projects').json()
        project=next(item for item in projects['projects'] if item['name']=='hud-preview')
        assert 'specs-v2' in project['description']
        page.locator('#hud-chat-tab').click()
        page.wait_for_function("document.querySelector('#project-hud').dataset.scene==='chat'")
        assert 'specs-v2' in page.locator('.hud-message-user').last.inner_text()
        page.locator('#hud-input').fill('把简介改成用于测试的简介')
        page.locator('#hud-send').click()
        page.locator('#hud-todos-tab').click()
        page.wait_for_selector('.hud-proposal',timeout=20000)
        proposal_text=page.locator('.hud-proposal').inner_text()
        assert '修改位置：项目设置' in proposal_text
        assert 'specs-v2' in proposal_text
        assert '用于测试的简介' in proposal_text
        page.screenshot(path=str(args.evidence/'hud-change-comparison.png'))
        page.set_viewport_size({'width':390,'height':844})
        for theme in ('dark','light'):
            page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
            box=page.locator('.hud-proposal').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=390.5
            page.screenshot(path=str(args.evidence/f'hud-change-mobile-{theme}.png'))
        page.get_by_role('button',name='暂不处理',exact=True).click()
        page.wait_for_function("!document.querySelector('.hud-proposal')",timeout=20000)
        page.locator('#hud-chat-tab').click()
        page.set_viewport_size({'width':1440,'height':1000})
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.locator('#hud-input').fill('怎么更换维护助手？')
        page.locator('#hud-send').click()
        page.wait_for_selector('#hud-content .hud-guide')
        assert page.locator('.hud-guide li').count()==3
        assert page.locator('.hud-option').count()==0
        page.screenshot(path=str(args.evidence/'hud-guide.png'))
        page.set_viewport_size({'width':390,'height':844})
        for theme in ('dark','light'):
            page.evaluate("theme=>document.documentElement.dataset.theme=theme",theme)
            box=page.locator('#project-hud').bounding_box()
            send=page.locator('#hud-send').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=390.5
            assert send['y']+send['height']<=844
            page.screenshot(path=str(args.evidence/('hud-guide-mobile-'+theme+'.png')))
        page.set_viewport_size({'width':1440,'height':1000})
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.locator('#hud-chat-tab').click()
        assert page.locator('#project-hud').get_attribute('data-scene')=='chat'
        assert '怎么更换维护助手' in page.locator('.hud-message-user').last.inner_text()
        page.locator('#hud-todos-tab').click()
        assert page.locator('#hud-todos').is_visible()
        page.locator('#hud-chat-tab').click()
        page.locator('#hud-input').fill('知道了，继续交流。')
        page.locator('#hud-send').click()
        page.wait_for_function("document.querySelector('#project-hud').dataset.scene==='chat'&&document.querySelector('#hud-input').value===''")
        page.evaluate("openProjectImport({mode:'new'})")
        page.wait_for_selector('#dlg-project-import[open] #project-hud')
        page.wait_for_function("document.querySelector('#hud-toggle').getAttribute('aria-expanded')==='true'")
        for width in (1440,1400,1390,853,390):
            page.set_viewport_size({'width':width,'height':844})
            page.locator('#hud-toggle').click()
            original_wizard=page.locator('#dlg-project-import').bounding_box()
            page.locator('#import-name').fill('可恢复的项目草稿')
            page.locator('#hud-toggle').click()
            page.wait_for_timeout(100)
            for theme in ('dark','light'):
                page.evaluate("theme => document.documentElement.dataset.theme=theme",theme)
                wizard=page.locator('#dlg-project-import').bounding_box()
                body=page.locator('.import-body').bounding_box()
                footer=page.locator('.import-footer').bounding_box()
                hud=page.locator('#project-hud').bounding_box()
                assert wizard==original_wizard,(wizard,original_wizard)
                assert body['height']>=80,body
                assert footer['y']+footer['height']<=844,footer
                assert hud['x']>=0 and hud['x']+hud['width']<=width,hud
                if width-wizard['x']-wizard['width']-16>=300:
                    assert abs(wizard['x']+wizard['width']+8-hud['x'])<1,(wizard,hud)
                    assert abs(hud['y']-wizard['y'])<1
                    assert abs(hud['height']-wizard['height'])<1
                    assert page.locator('#project-hud').evaluate('(e)=>e.classList.contains("hud-docked")')
                else:
                    assert not page.locator('#project-hud').evaluate('(e)=>e.classList.contains("hud-docked")')
                send=page.locator('#hud-send').bounding_box()
                assert send['y']+send['height']<=844,send
                assert page.locator('.hud-scroll').bounding_box()['height']>=50
                page.screenshot(path=str(args.evidence/f'hud-wizard-{width}-{theme}.png'))
            page.locator('#hud-input').focus()
            page.keyboard.press('Escape')
            assert page.locator('#dlg-project-import').evaluate('(e)=>e.open')
            assert page.locator('#hud-panel').is_hidden()
            assert page.locator('#import-name').input_value()=='可恢复的项目草稿'
            page.locator('#hud-toggle').click()
        page.set_viewport_size({'width':1440,'height':1000})
        page.evaluate("document.documentElement.dataset.theme='dark'")
        page.locator('#import-name').fill('可恢复的项目草稿')
        page.locator('#import-description').fill('创建算法评测项目，资料放在自定义目录。')
        page.locator('#import-template').select_option('algorithm')
        page.wait_for_timeout(1400)
        assert page.locator('#import-template').input_value()=='algorithm'
        page.screenshot(path=str(args.evidence/'hud-wizard.png'))
        page.locator('#hud-toggle').click()
        page.locator('#import-close').click()
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_timeout(1000)
        if page.locator('#hud-toggle').get_attribute('aria-expanded')!='true':page.locator('#hud-toggle').click()
        for theme in ('dark','light'):
            page.evaluate("theme => document.documentElement.dataset.theme=theme",theme)
            box=page.locator('#project-hud').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=390.5,box
            send=page.locator('#hud-send').bounding_box()
            assert send['y']+send['height']<=844,send
            page.screenshot(path=str(args.evidence/('hud-mobile-'+theme+'.png')))
        page.locator('#hud-input').focus()
        page.keyboard.press('Escape')
        assert page.locator('#hud-panel').is_hidden()
        assert page.locator('#hud-toggle').evaluate('(e)=>e===document.activeElement')
        page.evaluate("S.currentProject=null;renderProjectBar();ProjectHUD.changed()")
        page.wait_for_function("!document.querySelector('#hud-context').textContent.startsWith('HUD 交互预览')")
        page.evaluate("openProjectDetail('hud-preview',document.querySelector('#chip-all'))")
        page.wait_for_function("document.querySelector('#hud-context').textContent.startsWith('HUD 交互预览')")
        if page.locator('#hud-toggle').get_attribute('aria-expanded')=='true':page.locator('#hud-toggle').click()
        page.locator('#pj-d-desc').fill('窄屏未保存的项目配置')
        page.locator('#hud-toggle').click()
        page.locator('#hud-input').fill('继续检查项目资料')
        page.locator('#hud-send').click()
        page.wait_for_function("document.querySelector('#hud-input').value===''")
        assert page.locator('#dlg-project-detail').evaluate('(e)=>e.open')
        page.locator('#hud-input').focus()
        page.keyboard.press('Escape')
        assert page.locator('#dlg-project-detail').evaluate('(e)=>e.open')
        assert page.locator('#pj-d-desc').input_value()=='窄屏未保存的项目配置'
        page.locator('#dlg-pj-detail-close').click()
        page.wait_for_function("!document.querySelector('#hud-context').textContent.startsWith('HUD 交互预览')")
        # Previously persisted free-text questions may omit the options field.
        def free_text_question(route):
            response=route.fetch()
            data=response.json()
            if route.request.method=='GET' and isinstance(data,dict) and 'state' in data:
                data['status']='waiting'
                data['state'].pop('proposal',None)
                data['state']['question']={'id':'free-text-regression','title':'这个项目要做成什么？',
                                           'reason':'项目目标决定需要哪些输入和产出。'}
                route.fulfill(response=response,json=data)
            else:
                route.fulfill(response=response)
        page.route(re.compile(r'/hud/[^/?]+$'),free_text_question)
        page.reload()
        page.locator('#hud-toggle').click()
        page.wait_for_selector('.hud-question',state='attached')
        page.locator('#hud-todos-tab').click()
        page.wait_for_selector('.hud-question')
        assert page.locator('.hud-option').count()==0
        assert page.locator('#hud-input').is_enabled()
        page.locator('#hud-input').fill('整理研究资料并形成决策报告')
        assert page.locator('#hud-send').is_enabled()
        page.screenshot(path=str(args.evidence/'hud-free-text.png'))
        page.unroute(re.compile(r'/hud/[^/?]+$'), free_text_question)
        progress_state={'kind':'stage','text':'正在读取项目资料','started':time.time(),'updated':time.time()}
        progress_status={'value':'running'}
        def progress_response(route):
            response=route.fetch()
            data=response.json()
            if route.request.method=='GET' and isinstance(data,dict) and 'state' in data:
                data['status']=progress_status['value']
                data['state'].pop('question',None)
                data['state'].pop('proposal',None)
                data['state']['progress']=dict(progress_state)
                data['error']='Hermes 运行环境缺少依赖（dotenv），请检查所选宿主的 Python 环境' if data['status']=='error' else ''
            route.fulfill(response=response,json=data)
        page.route(re.compile(r'/hud/[^/?]+$'), progress_response)
        page.evaluate('ProjectHUD.changed()')
        page.wait_for_function("document.querySelector('#hud-progress-text').textContent==='正在读取项目资料'")
        progress_state.update(kind='reasoning',text='核对已有需求中的项目目标、约束与验收条件。'*15+'最新：确认缺少的验收标准',updated=time.time())
        page.wait_for_function("document.querySelector('#hud-progress-text').textContent.endsWith('最新：确认缺少的验收标准')")
        line=page.locator('#hud-progress-text')
        assert line.evaluate('(e)=>e.scrollLeft>0 && e.scrollHeight<=e.clientHeight+1')
        first_elapsed=page.locator('#hud-progress-time').inner_text()
        page.wait_for_function("value=>document.querySelector('#hud-progress-time').textContent!==value",arg=first_elapsed,timeout=5000)
        for theme in ('dark','light'):
            page.evaluate('theme=>document.documentElement.dataset.theme=theme',theme)
            page.screenshot(path=str(args.evidence/f'hud-progress-mobile-{theme}.png'))
        page.reload()
        page.wait_for_function("!document.querySelector('#hud-progress')?.hidden")
        if page.locator('#hud-toggle').get_attribute('aria-expanded')!='true':page.locator('#hud-toggle').click()
        page.wait_for_selector('#hud-progress:not([hidden])')
        assert line.inner_text().endswith('最新：确认缺少的验收标准')
        progress_status['value']='error'
        page.wait_for_selector('#hud-error:not([hidden])')
        assert page.locator('#hud-progress').is_hidden()
        assert '缺少依赖（dotenv）' in page.locator('#hud-error').inner_text()
        assert not page.locator('#project-hud').evaluate('(e)=>e.classList.contains("hud-running")')
        page.screenshot(path=str(args.evidence/'hud-progress-error.png'))
        assert not errors,errors
        (args.evidence/'browser.json').write_text(json.dumps({'passed':True,'errors':errors,'checks':[
            'automatic question expansion','all detail tabs retain identical window geometry','assistant and sent user text visible in conversation','options send as user message and disappear','conversation and todo tabs with question/proposal cards and inline guide','guide history and free-text follow-up','detail position unchanged; HUD docks at equal height and returns on collapse','project details stay open with unsaved fields and active tab','options and custom text','refresh preserves input','confirmed facts',
            'wizard uses detail docking rule and floating fallback; geometry and draft preserved','wizard draft input and template selection','390px dark/light','Escape and focus','detail project context and mobile reply','free-text question without options','same-revision live reasoning summary; single latest line; elapsed time; reload; failure stops progress']},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()


if __name__=='__main__':main()
