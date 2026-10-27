"""Real-browser HUD checks against preview_project_hud.py's isolated fixture."""
import argparse
import json
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
        page.screenshot(path=str(args.evidence/'hud-desktop.png'))
        page.get_by_role('radio').first.click()
        page.locator('#hud-input').fill('需求放在 specs-v2，原型放在 design-sketches。')
        page.reload()
        page.wait_for_selector('#hud-input')
        assert 'specs-v2' in page.locator('#hud-input').input_value()
        assert page.get_by_role('radio').first.get_attribute('aria-checked')=='true'
        page.locator('#hud-send').click()
        page.wait_for_selector('.hud-proposal',timeout=20000)
        page.get_by_role('button',name='确认保存维护资料',exact=True).click()
        page.wait_for_function("!document.querySelector('.hud-proposal')",timeout=20000)
        page.locator('.hud-history summary').click()
        assert 'specs-v2' in page.locator('#hud-history').inner_text()
        page.evaluate("openProjectImport({mode:'new'})")
        page.wait_for_selector('#dlg-project-import[open] #project-hud')
        # Collapse exposes the wizard while keeping its persistent HUD entry.
        if page.locator('#hud-toggle').get_attribute('aria-expanded')=='true':page.locator('#hud-toggle').click()
        page.locator('#import-name').fill('可恢复的项目草稿')
        page.locator('#import-description').fill('创建算法评测项目，资料放在自定义目录。')
        page.locator('#import-template').select_option('algorithm')
        page.wait_for_timeout(1400)
        page.locator('#import-close').click()
        page.wait_for_timeout(3500)
        page.locator('#hud-toggle').click()
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
        assert not errors,errors
        (args.evidence/'browser.json').write_text(json.dumps({'passed':True,'errors':errors,'checks':[
            'automatic question expansion','options and custom text','refresh preserves input','confirmed facts',
            'wizard draft restore','390px dark/light','Escape and focus']},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()


if __name__=='__main__':main()
