"""req#110 #189：网页内目录选择器替代原生对话框（方案A）——Chromium 合成接口，无生产写入。
覆盖：①选择器打开（导入弹窗入口+设置弹窗入口）导航/面包屑/选目录回填；②无权限/空目录/大目录分页边界；
③取消后重选；④503 错误态；⑤385px 弹窗自身布局；不再触达 /local/directory-picker 原生对话框接口。
"""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    (ROOT / 'cache').mkdir(exist_ok=True)
    TREE = {
        'E:/': ['code', 'docs', '中文 目录', 'system-locked', 'big'],
        'E:/code': ['bot-group', '空 目录x'],
        'E:/code/bot-group': ['frontend', 'engine'],
        'E:/code/空 目录x': [],
        'E:/中文 目录': [],
        'E:/big': [f'd{i:02d}' for i in range(25)],
        'C:/': ['Users'],         # ④b 换盘：C: 有真实子目录 → 进入+确认回填成功（非 not_found）
        'C:/Users': ['developer'],
        'C:/Users/developer': ['Desktop'],
        # E:/docs / E:/system-locked 故意不进 TREE → not_found / permission_denied
    }
    DISKS = ['C:/', 'E:/']  # req#110 #189 验收退回②：根视图多盘符（换盘导航用例）
    state = {'fail': None}

    def data(path, offset=0, limit=200):
        if not path:
            return {'path': '', 'parent': None,
                    'entries': [{'name': d, 'path': d, 'navigable': True} for d in DISKS],
                    'total': len(DISKS), 'offset': 0, 'limit': limit, 'has_more': False}
        norm = (path if path == 'E:/' or path == 'C:/' else path.rstrip('/') or '/')
        kids = TREE.get(norm)
        if kids is None:
            return {'path': path, 'parent': 'E:', 'entries': [], 'total': 0, 'offset': offset,
                    'limit': limit, 'has_more': False,
                    'error': 'permission_denied' if 'locked' in path else 'not_found'}
        base = norm + '/' if norm not in ('E:/', 'C:/') else norm
        # 目录视图模拟后端 os.path.join 的反斜杠拼接结果，
        # entries 的 path 末段带反斜杠（E:\Code\bot-group\frontend），接口回显 path 同样反斜杠
        if norm == 'E:/code/bot-group':
            return {'path': 'E:\\code\\bot-group', 'parent': 'E:/code', 'limit': limit, 'offset': offset,
                    'total': 2, 'has_more': False,
                    'entries': [{'name': 'frontend', 'path': 'E:\\code\\bot-group\\frontend', 'navigable': True},
                                {'name': 'engine', 'path': 'E:\\code\\bot-group\\engine', 'navigable': True}]}
        all_e = [{'name': k, 'path': base + k, 'navigable': True} for k in kids]
        total = len(all_e)
        pg = all_e[offset:offset + limit]
        crumb_parent = 'E:/code' if norm == 'E:/code/空 目录x' else (
            ('E:/' if norm.startswith('E:') else 'C:/') if norm.count('/') == 1
            else (norm.rsplit('/', 1)[0] + '/' if norm.count('/') > 1 else None))
        return {'path': norm, 'parent': crumb_parent, 'entries': pg, 'total': total,
                'offset': offset, 'limit': limit, 'has_more': offset + len(pg) < total}

    # 大目录分页：mock 对 big 目录（25 项）固定返回 limit=10 分页（前端按 has_more 渲染「加载更多」）
    def data_paged(path, offset=0, limit=200):
        if path.rstrip('/') == 'E:/big':
            return data(path, offset, 10)
        return data(path, offset, limit)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 768})  # 高度 768：居中/超屏断言（④c）回归锚
        errors = []
        page.on('pageerror', lambda error: errors.append(getattr(error, 'stack', None) or str(error)))
        def route(request):
            path = urlparse(request.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if path == '/local/directory-list':
                # req#110 #189：网页选择器走「列目录」接口；原生 directory-picker 接口不再被调用
                if state['fail'] == 'net':
                    request.fulfill(status=503, json={'detail': 'offline'})
                    return
                q = parse_qs(urlparse(request.request.url).query)
                request.fulfill(json=data_paged((q.get('path') or [''])[0],
                                                int((q.get('offset') or [0])[0]),
                                                int((q.get('limit') or [200])[0])))
                return
            request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [],
                                  'requirements': [], 'tasks': [], 'pending_approval': []})

        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.evaluate('showLogin(false); openProjectImport()')

        def rows():
            return page.locator('#dir-picker-list [role=option]')

        def open_picker():
            page.locator('#import-pick-root').click()
            page.locator('#dir-picker-ok').last.wait_for(state='visible')  # 上一 picker 未清场时 last=新开弹窗
            # dbg83/dbg90 判据=「加载完成三态」：rows>0（有子目录/盘符）或「无子目录」空态或 status 非读取中
            # 输入框可能指向任意视图，包括含 Desktop 子目录的用户目录。
            page.wait_for_function("(() => { const dlg = [...document.querySelectorAll('dialog[dir-picker-layer]')].pop(); if (!dlg || !dlg.open) return false; const l = [...dlg.querySelectorAll('#dir-picker-list')].pop(); const more = (l && l.querySelectorAll('[role=option]').length > 0) || (l && l.textContent.includes('无子目录')); return more || !dlg.querySelector('#dir-picker-status').textContent.includes('正在读取'); })()")

        def goto_root():
            # 新实现：固定「服务器根目录」chip = root 入口（盘符列表 rows=C:+E=2）；
            # E:/ chip 是「E:/ 目录」（rows=5），语义已分家（req#110 #189 退回②实现）
            page.locator('#dir-picker-crumbs button', has_text='服务器根目录').first.click()
            page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")

        def pick_disk_e():
            # root 视图（rows=2）点 E:/ 行进 E: 子列表（rows=5）
            page.locator('#dir-picker-list [role=option]', has_text='E:/').first.click()
            page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 5")

        # —— ① 打开+导航+面包屑+选目录回填（中文空格路径）——
        open_picker()
        assert '服务器目录' in page.locator('#dir-picker-title').inner_text()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('E:/')")
        pick_disk_e()  # root(_rows=2 C:/+E:/) → E:/（rows=5）——DISKS 多盘符后第一行不再必然是 E:/
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 5")
        page.locator('#dir-picker-list [role=option]', has_text='code').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")
        crumbs = page.locator('#dir-picker-crumbs').inner_text()
        assert 'E:/' in crumbs and 'code' in crumbs, crumbs
        goto_root()
        pick_disk_e()  # root→E:/（rows=5），中文 目录 在 E: 下
        # 单击行进入目录，底部「选择此目录」提交当前位置。
        page.locator('#dir-picker-list [role=option]', has_text='中文 目录').first.click()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('无子目录')")
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#import-root') == 'E:/中文 目录'

        # —— ② 无权限/空目录/大目录分页 ——
        open_picker()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('无子目录')")
        goto_root()
        pick_disk_e()  # _rows=2（C:/+E:/）→ E:/（rows=5）
        page.locator('#dir-picker-list [role=option]', has_text='system-locked').first.click()
        page.wait_for_function("document.querySelector('#dir-picker-status').textContent.includes('无权限')")
        page.locator('#dir-picker-crumbs button', has_text='E:').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 5")
        # 大目录分页（big mock 固定 limit=10 一页）：E:/ → big
        page.locator('#dir-picker-list [role=option]', has_text='big').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 10")
        assert page.locator('#dir-picker-more button', has_text='加载更多').count() == 1
        page.locator('#dir-picker-more button', has_text='加载更多').click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 20")
        page.locator('#dir-picker-more button', has_text='加载更多').click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 25")
        assert page.locator('#dir-picker-more button').count() == 0, '末页不应再有「加载更多」'
        page.locator('#dir-picker-cancel').last.click()  # 验证显式取消按钮关闭弹窗。
        page.wait_for_timeout(150)

        # —— ③ 关闭后重选（值不被破坏）——
        assert page.input_value('#import-root') == 'E:/中文 目录', '关闭不改值'
        open_picker()
        goto_root()
        pick_disk_e()  # root→E:/（rows=5），code 在 E: 下
        page.locator('#dir-picker-list [role=option]', has_text='code').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")
        # 进入空目录后通过底部按钮确认。
        page.locator('#dir-picker-list [role=option]', has_text='空 目录x').first.click()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('无子目录')")
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#import-root') == 'E:/code/空 目录x'
        # 注入监听器，验证 finish 派发 input/change 事件。
        page.evaluate("window.__ev189 = []; const inp = document.querySelector('#import-root'); "
                      "inp.addEventListener('change', e => window.__ev189.push('change')); "
                      "inp.addEventListener('input', e => window.__ev189.push('input'));")
        # —— ③b req#110 #189 验收退回①：进入空目录后底部「选择此目录」可确认回填 + 派事件 ——
        # （承接 ③：input 已回填 E:/code/空 目录x，重新打开按设计直落当前值目录=空目录视图）
        open_picker()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('无子目录')")
        assert page.locator('#dir-picker-status').inner_text() == '', '空目录加载成功不应有状态文案'
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#import-root') == 'E:/code/空 目录x', '空目录底部确认应回填当前目录'
        # 目录接口回显 os.path.join 生成的反斜杠路径，
        # 点行 navigate（entry.path 带反斜杠）→ 底部确认 finish(currentPath) 回填必须统一正斜杠
        open_picker()
        goto_root()
        pick_disk_e()  # root rows=2（C:/+E:/）→ E:/ rows=5（code 在 E: 下）
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('code')")
        page.locator('#dir-picker-list [role=option]', has_text='code').first.click()
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('bot-group')")
        page.locator('#dir-picker-list [role=option]', has_text='bot-group').first.click()
        page.wait_for_function("() => { const l=[...document.querySelectorAll('#dir-picker-list')].pop(); return l && l.textContent.includes('frontend'); }")
        assert 'E:' in page.locator('#dir-picker-crumbs').inner_text()  # 面包屑仍可读（反斜杠路径渲染不炸）
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#import-root') == 'E:/code/bot-group', '斜杠应统一成正斜杠，不混用反斜杠'
        events = page.evaluate("() => window.__ev189")
        assert 'input' in events and 'change' in events, f'finish 应回填并派事件，实际 {events}'

        # —— ④ 错误态（503）：状态提示不崩溃，重试恢复 ——
        open_picker()
        state['fail'] = 'net'
        page.locator('#dir-picker-crumbs button', has_text='E:/').first.click()
        page.wait_for_function("document.querySelector('#dir-picker-status').textContent.includes('读取失败')")
        state['fail'] = None
        page.locator('#dir-picker-crumbs button', has_text='E:/').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 5")
        page.keyboard.press('Escape')
        page.wait_for_timeout(150)

        # —— ④b req#110 #189 验收退回②：从已有盘符一键回「服务器根目录」换盘（E:/ → C:/）——
        open_picker()
        goto_root()
        pick_disk_e()  # root→E:/（rows=5），code 在 E: 下
        page.locator('#dir-picker-list [role=option]', has_text='code').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")
        # 任何层级都有固定「服务器根目录」chip → 点击回盘符列表
        page.locator('#dir-picker-crumbs button', has_text='服务器根目录').first.click()
        page.wait_for_function("[...document.querySelectorAll('#dir-picker-list [role=option]')].some(r => r.textContent.includes('C:/'))")
        # 换盘完整链：进入磁盘、逐层导航到用户目录、通过底部按钮确认回填。
        page.locator('#dir-picker-list [role=option]', has_text='C:/').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 1")
        page.locator('#dir-picker-list [role=option]', has_text='Users').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 1")
        page.locator('#dir-picker-list [role=option]', has_text='developer').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 1")  # 模拟用户目录包含 Desktop，进入后确认。
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#import-root') == 'C:/Users/developer', '换盘后进目录+底部确认应回填'

        # 验证居中且位于视口内、外部点击关闭、唯一确认按钮。
        open_picker()
        dlg_box = page.locator('dialog[dir-picker-layer]').bounding_box()
        assert dlg_box['x'] >= 0 and dlg_box['y'] >= 0, f'④c 弹窗不得超视口左侧/顶部 {dlg_box}'
        assert dlg_box['x'] + dlg_box['width'] <= 1280 + 1 and dlg_box['y'] + dlg_box['height'] <= 768 + 1, \
            f'④c 弹窗不得超视口右/下 {dlg_box}'  # 视口 1280x768（小屏超屏回归锚）
        assert dlg_box['width'] == 520, f'④c 居中宽度不变 {dlg_box}'
        cx, cy = dlg_box['x'] + dlg_box['width'] / 2, dlg_box['y'] + dlg_box['height'] / 2
        assert abs(cx - 640) <= 2 and abs(cy - 384) <= 2, f'④c 弹窗应居中 {cx},{cy}'
        assert page.locator('#dir-picker-cancel').count() == 1, '④c 取消按钮保留（6384 定案）'
        assert page.locator('#dir-picker-ok').count() == 1, '④c 仅保留「选择此目录」一个选择按钮'
        assert page.locator('#dir-picker-list [role=option] button').count() == 0, '④c 行内「选这里」已移除'
        # 取消后保留原输入值。
        page.locator('#dir-picker-cancel').last.click()
        page.wait_for_function("() => [...document.querySelectorAll('dialog[dir-picker-layer]')].length === 0")
        assert page.input_value('#import-root') == 'C:/Users/developer', '取消不改值'
        # 外点关闭（点弹窗外）
        open_picker()
        page.mouse.click(30, 30)  # 页面空白处（弹窗外）= 关闭
        page.wait_for_function("() => [...document.querySelectorAll('dialog[dir-picker-layer]')].length === 0")

        # —— ⑤ 第二入口：项目设置弹窗（真实路径：当前项目 chip → 详情弹窗）pj-d-root 的「选择项目主目录」同样开网页选择器 ——
        page.evaluate("() => { document.querySelector('#dlg-project-import').close(); S.projects = [{name: 'p1', stable_id: 'sid-1', root: '', members: [], agents: []}]; S.currentProject = 'p1'; renderProjectBar(); }")
        page.locator('.project-chip[data-project="p1"]').click()
        page.wait_for_function("document.querySelector('#dlg-project-detail').open")
        page.locator('[aria-label="选择项目主目录"]').click()
        page.locator('#dir-picker-ok').wait_for(state='visible')
        page.wait_for_function("document.querySelector('#dir-picker-list').textContent.includes('E:/')")
        pick_disk_e()  # 进 E:/（root 第一行现为 C:/，显式点 E:/）
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 5")
        page.locator('#dir-picker-list [role=option]', has_text='code').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")
        # 进入含 frontend、engine 两行的目录视图，再通过底部按钮确认回填。
        page.locator('#dir-picker-list [role=option]', has_text='bot-group').first.click()
        page.wait_for_function("document.querySelectorAll('#dir-picker-list [role=option]').length === 2")
        page.locator('#dir-picker-ok').click()
        page.wait_for_function("() => !document.querySelector('#dir-picker-ok')")
        assert page.input_value('#pj-d-root') == 'E:/code/bot-group'

        # —— ① 触发按钮本体（36px 图标钮）不回归 ——
        btn = page.locator('[aria-label="选择项目主目录"]')
        box = btn.bounding_box()
        assert box['width'] == 36 and box['height'] == 36
        assert btn.locator('svg').count() == 1
        page.evaluate("document.querySelector('#pj-d-root').closest('dialog').close()")

        # 动态居中回归：内容增高触发 ResizeObserver，视口缩放触发 resize 监听。
        # 上一步已关项目详情主弹窗，这里重开导入向导（第一入口）作载体
        page.evaluate('openProjectImport()')
        def dlgbox():
            return page.evaluate("(() => { const dlg = [...document.querySelectorAll('dialog[dir-picker-layer]')].pop(); const r = dlg.getBoundingClientRect(); return {top: r.top, h: r.height, bottom: r.bottom, right: r.right, cx: r.left + r.width/2, cy: r.top + r.height/2}; })()")
        open_picker()
        page.wait_for_timeout(220)  # 初始 place + 稳定
        b_root = dlgbox()           # 根视图 2 行基准
        pick_disk_e()               # 进 E:/（rows=5 内容增高）
        page.wait_for_timeout(300)  # ResizeObserver 回调落地
        b_big = dlgbox()
        assert abs(b_big['cy'] - page.evaluate('innerHeight') / 2) < 2, f'增高后应垂直重居中 cy={b_big["cy"]}（期望 viewport 中线）'
        assert b_big['cy'] <= (page.evaluate('innerHeight')/2 + 2) and b_big['bottom'] <= page.evaluate('innerHeight'), '增高后超屏'
        page.set_viewport_size({'width': 1024, 'height': 680})
        page.wait_for_timeout(250)
        b_small = dlgbox()
        assert abs(b_small['cx'] - 512) < 4, f'缩放后应水平重定位 cx={b_small["cx"]}'
        assert b_small['bottom'] <= 680.01 and b_small['right'] <= 1024.01, '缩放后底部/右侧超屏'
        page.locator('#dir-picker-cancel').last.click()
        page.wait_for_function("() => [...document.querySelectorAll('dialog[dir-picker-layer]')].length === 0")
        page.set_viewport_size({'width': 1280, 'height': 768})

        assert not errors, errors
        page.screenshot(path=str(ROOT / 'cache/directory-picker-web.png'))
        browser.close()
    print('PASS: web directory picker (req#110 #189 复工), centered no-clip, recenter on grow + resize, no row buttons, keep cancel, outside-click close, normalized slash backfill, navigation, breadcrumbs, pick/repick, denied/empty/paged, 503, both entries')


if __name__ == '__main__':
    main()
