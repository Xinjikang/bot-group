"""Real Chrome appearance checks with synthetic API data; no production writes."""
import io
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image
from playwright.sync_api import sync_playwright
from background_test_support import BackgroundAPI
from engine import appearance

ROOT = Path(__file__).resolve().parents[1]


def saved_background(page):
    return appearance.read() or None


def main():
    output = ROOT / 'cache' / 'appearance-browser'
    output.mkdir(parents=True, exist_ok=True)
    with BackgroundAPI() as background, sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        def route(request):
            path = urlparse(request.request.url).path
            if path == '/appearance/background':
                background.route(request)
            elif path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(json={'threads': [], 'messages': [], 'members': [], 'projects': [], 'tasks': [], 'requirements': []})
        page.route('**/*', route)
        page.goto('http://appearance.test/app/index.html')
        page.evaluate('showLogin(false)')
        assert page.locator('#topbar .logo').inner_text().startswith('BOT GROUP')
        # Render representative content through the real app, entirely in memory.
        page.evaluate('''() => {
            S.currentProject = 'preview'; S.isUserIdentity = true;
            S.projects = [{name:'preview', display_name:'bot-group'}];
            S.members = [
                {profile:'architect', display_name:'知夏'},
                {profile:'developer', display_name:'苏瑾'}
            ];
            S.threads = [
                {id:71, project:'preview', title:'浅色舱室 · 外观适配', status:'open',
                 members:'["architect","developer","user"]', first_message_id:1, created_at:'2026-10-16 10:30:00'},
                {id:72, project:'preview', title:'关于 adapter 的去留', status:'open',
                 members:'["architect","developer"]', created_at:'2026-10-15 16:20:00'},
                {id:73, project:'preview', title:'消息导航与固定操作区', status:'closed',
                 members:'["architect","user"]', created_at:'2026-10-14 09:30:00'},
                {id:74, project:'preview', title:'项目配置与工作区资源', status:'closed',
                 members:'["developer","user"]', created_at:'2026-10-13 14:15:00'}
            ];
            S.msgs = [
                {id:1, sender:'user', sender_type:'user', content:'浅色也要保留科技感，飞船内部和舷窗玻璃的质感都要体现出来。'},
                {id:2, sender:'architect', sender_type:'bot', mentions:'["developer"]',
                 content:'@developer 沿用深色主题的舱体结构、香槟金边框和青色指示光。浅色版调整面板明度与玻璃透光率，保持同一套视觉语言。\\n\\n侧栏呈现银灰金属的层次，聊天区保留窗外行星的明暗与景深。阅读区域采用轻磨砂玻璃，消息正文保持清晰。'},
                {id:3, sender:'developer', sender_type:'bot',
                 content:'舷窗增加内沿、边缘高光和局部反射。输入区沿用原有控制台造型，选中态继续使用金色。\\n\\n自定义背景、主题切换和原有操作位置保持一致，长消息与任务面板一起检查。'},
                {id:4, sender:'user', sender_type:'user', content:'先做一版，看看实际页面的效果。'}
            ].map((m, i) => ({...m, project:'preview', thread_id:71,
                mentions:m.mentions || '[]', ts:'2026-10-16 10:3' + i + ':00', attachments:[]}));
            S.messagesReady = true;
            renderProjectBar(); renderThreads(); renderMessages(); renderTasks([], []);
            document.querySelector('#conn-text').textContent = '本地预览';
        }''')
        planet_geometry = {}
        for theme in ('dark', 'light'):
            page.evaluate('(t) => applyTheme(t)', theme)
            bg = page.locator('.col-msgs').evaluate('(e) => getComputedStyle(e).backgroundImage')
            assert 'viewport-planet.jpg' in bg
            planet_geometry[theme] = page.locator('.col-msgs').evaluate('''e => {
                const style = getComputedStyle(e), rect = e.getBoundingClientRect();
                return {size:style.backgroundSize.split(',')[0].trim(),
                    position:style.backgroundPosition.split(',')[0].trim(),
                    width:rect.width, height:rect.height};
            }''')
            if theme == 'light':
                # The decorative lens must never intercept the topic or input controls.
                assert page.locator('.col-msgs').evaluate('(e) => getComputedStyle(e,"::after").pointerEvents') == 'none'
                head = page.locator('.msg-group-head').first
                head.click()
                assert page.locator('.msg-group').first.evaluate('(e) => e.classList.contains("collapsed")')
                head.click()
                page.locator('#composer textarea').fill('玻璃边缘覆盖下，输入仍可正常操作。')
                assert page.locator('#composer textarea').input_value().startswith('玻璃边缘')
                page.locator('#composer textarea').fill('')
                page.locator('#composer textarea').blur()
            page.screenshot(path=str(output / f'default-{theme}.png'))
        assert planet_geometry['dark'] == planet_geometry['light'], 'theme switching must preserve planet scale and position'
        page.locator('#btn-background').click()
        page.wait_for_function('!document.querySelector("#background-select").disabled')
        image = io.BytesIO()
        Image.new('RGB', (64, 64), '#778899').save(image, format='PNG')
        upload = {'name': 'custom.png', 'mimeType': 'image/png', 'buffer': image.getvalue()}
        page.locator('#background-file').set_input_files(upload)
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("背景已保存")')
        saved = saved_background(page)
        assert saved == image.getvalue()
        assert page.evaluate('localStorage.getItem("ge_viewport_background")') is None
        for theme in ('dark', 'light'):
            page.evaluate('(t) => applyTheme(t)', theme)
            assert 'blob:' in page.locator('.col-msgs').evaluate('(e) => getComputedStyle(e).backgroundImage')
        page.reload()
        page.evaluate('showLogin(false)')
        page.wait_for_function('!document.querySelector("#background-select").disabled')
        assert 'blob:' in page.locator('.col-msgs').evaluate('(e) => getComputedStyle(e).backgroundImage')
        page.locator('#btn-background').click()
        page.wait_for_function('!document.querySelector("#background-select").disabled')
        page.locator('#background-file').set_input_files({'name': 'large.png', 'mimeType': 'image/png', 'buffer': b'x' * (11 * 1024 * 1024)})
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("图片过大")')
        assert saved_background(page) == saved
        page.locator('#background-file').set_input_files({'name': 'bad.png', 'mimeType': 'image/png', 'buffer': b'broken'})
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("无法解码")')
        background.fail = True
        page.locator('#background-file').set_input_files(upload)
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("服务端图片保存失败")')
        assert saved_background(page) == saved
        assert page.locator('#background-status').get_attribute('role') == 'alert'
        background.fail = False
        page.locator('#background-reset').click()
        page.wait_for_function('document.querySelector("#background-status").textContent.includes("已恢复默认")')
        assert saved_background(page) is None
        assert page.evaluate('localStorage.getItem("ge_viewport_background")') is None
        page.locator('#background-close').click()
        for theme in ('light', 'dark'):
            page.evaluate('(t) => applyTheme(t)', theme)
            assert 'viewport-planet.jpg' in page.locator('.col-msgs').evaluate('(e) => getComputedStyle(e).backgroundImage')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#btn-background').click()
        assert page.locator('#dlg-background').evaluate('(e) => e.getBoundingClientRect().right <= innerWidth')
        page.screenshot(path=str(output / 'settings-390.png'))
        assert not errors, errors
        browser.close()
    print('PASS: both themes retain planet, server background persistence/reset, size/decode/save failures, mobile')


if __name__ == '__main__':
    main()
