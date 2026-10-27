"""Real-browser HUD checks against preview_project_hud.py's isolated fixture."""
import argparse
import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8196')
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
        page.wait_for_selector('.hud-question',timeout=20000)
        assert '先把需求资料准备好' in page.locator('.hud-message-assistant').last.inner_text()
        assert page.locator('#project-hud').get_attribute('data-scene')=='choice'
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
            if width==1120:
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
        page.wait_for_selector('#hud-input')
        assert 'specs-v2' in page.locator('#hud-input').input_value()
        page.locator('.hud-option').first.click()
        page.wait_for_selector('.hud-proposal',timeout=20000)
        assert page.locator('.hud-option').count()==0
        assert page.locator('#project-hud').get_attribute('data-scene')=='confirm'
        page.screenshot(path=str(args.evidence/'hud-confirmation.png'))
        page.locator('#hud-scene-toggle').click()
        assert '已有完整文档' in page.locator('.hud-message-user').last.inner_text()
        assert 'specs-v2' in page.locator('.hud-message-user').last.inner_text()
        assert '资料登记建议' in page.locator('.hud-message-assistant').last.inner_text()
        assert page.locator('#hud-input').input_value()==''
        for selector in ('.hud-message-user .hud-message-text','.hud-message-assistant .hud-message-text'):
            assert page.locator(selector).last.evaluate("e=>{const a=e.getBoundingClientRect(),b=document.querySelector('.hud-scroll').getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom}")
        page.screenshot(path=str(args.evidence/'hud-conversation.png'))
        page.reload()
        page.wait_for_selector('.hud-proposal')
        assert page.locator('.hud-option').count()==0
        page.get_by_role('button',name='确认保存维护资料',exact=True).click()
        page.wait_for_function("!document.querySelector('.hud-proposal')",timeout=20000)
        page.locator('#hud-more').click()
        page.locator('#hud-history-toggle').click()
        assert 'specs-v2' in page.locator('#hud-history').inner_text()
        page.locator('#hud-back').click()
        page.wait_for_function("document.querySelector('#project-hud').dataset.scene==='chat'")
        assert 'specs-v2' in page.locator('.hud-message-user').last.inner_text()
        page.locator('#hud-input').fill('怎么更换维护助手？')
        page.locator('#hud-send').click()
        page.wait_for_selector('#project-hud[data-scene="guide"] .hud-guide')
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
        page.locator('#hud-scene-toggle').click()
        assert page.locator('#project-hud').get_attribute('data-scene')=='chat'
        assert '怎么更换维护助手' in page.locator('.hud-message-user').last.inner_text()
        page.locator('#hud-scene-toggle').click()
        assert page.locator('#project-hud').get_attribute('data-scene')=='guide'
        page.locator('#hud-input').fill('知道了，继续交流。')
        page.locator('#hud-send').click()
        page.wait_for_function("document.querySelector('#project-hud').dataset.scene==='chat'&&document.querySelector('#hud-input').value===''")
        page.evaluate("openProjectImport({mode:'new'})")
        page.wait_for_selector('#dlg-project-import[open] #project-hud')
        # Collapse exposes the wizard while keeping its persistent HUD entry.
        if page.locator('#hud-toggle').get_attribute('aria-expanded')=='true':page.locator('#hud-toggle').click()
        page.locator('#import-name').fill('可恢复的项目草稿')
        page.locator('#import-description').fill('创建算法评测项目，资料放在自定义目录。')
        page.locator('#import-template').select_option('algorithm')
        page.wait_for_timeout(1400)
        page.locator('#import-close').click()
        page.get_by_role('button',name='继续：可恢复的项目草稿',exact=True,include_hidden=True).wait_for(state='attached')
        if page.locator('#hud-toggle').get_attribute('aria-expanded')!='true':page.locator('#hud-toggle').click()
        page.locator('#hud-more').click()
        page.locator('#hud-projects-toggle').click()
        page.get_by_role('button',name='继续：可恢复的项目草稿',exact=True).click()
        page.wait_for_function("document.querySelector('#import-name').value==='可恢复的项目草稿'")
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
        page.wait_for_selector('.hud-question')
        assert page.locator('.hud-option').count()==0
        assert page.locator('#hud-input').is_enabled()
        page.locator('#hud-input').fill('整理研究资料并形成决策报告')
        assert page.locator('#hud-send').is_enabled()
        page.screenshot(path=str(args.evidence/'hud-free-text.png'))
        assert not errors,errors
        (args.evidence/'browser.json').write_text(json.dumps({'passed':True,'errors':errors,'checks':[
            'automatic question expansion','all detail tabs retain identical window geometry','assistant and sent user text visible in conversation','options send as user message and disappear','choice / confirmation / conversation / guide scene transitions','guide history and free-text follow-up','detail position unchanged; HUD docks at equal height and returns on collapse','project details stay open with unsaved fields and active tab','options and custom text','refresh preserves input','confirmed facts',
            'wizard draft restore','390px dark/light','Escape and focus','detail project context and mobile reply','free-text question without options']},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()


if __name__=='__main__':main()
