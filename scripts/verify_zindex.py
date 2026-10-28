"""全弹窗层级回归测试，覆盖弹窗堆叠及交互可达性。
六项：新建话题/全局成员/项目详情/全部项目下拉/话题⋯菜单/背景交互。
用法：python scripts/verify_zindex.py --url <隔离服务origin> --project <测试项目ID>
每项验证：完整在视口 + 右边缘 elementFromPoint 命中（不被任何栏盖住）。
注意：选择器必须 grep 代码实际 id（chip-all→#pj-dropdown 动态创建；菜单按钮=.tc-menu-btn）。
"""
import argparse
import os
from probe_config import loopback_url

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', type=loopback_url, required=True)
parser.add_argument('--project', required=True)
args = parser.parse_args()
from playwright.sync_api import sync_playwright

results = []
def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS" if ok else "❌FAIL"), name, detail)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width': 1400, 'height': 850})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(args.url + '/app/', wait_until='domcontentloaded')
    pg.wait_for_timeout(1500)
    if pg.locator('#login-pwd').is_visible():
        pg.locator('#login-pwd').fill(os.environ.get('PROBE_ADMIN_PASSWORD', ''))
        pg.keyboard.press('Enter')
    pg.wait_for_timeout(2500)

    def edge_hit(sel):
        box = pg.locator(sel).first.bounding_box()
        if not box: return None, None
        rx, ry = int(box['x']+box['width']-5), int(box['y']+max(5, box['height']//2))
        hit = pg.evaluate(f'document.elementFromPoint({rx},{ry})?.closest("{sel}") !== null')
        in_vp = box['x'] >= 0 and box['y'] >= 0 and box['x']+box['width'] <= 1400 and box['y']+box['height'] <= 850
        return in_vp, hit

    # 1 新建话题
    pg.locator('#btn-new-thread').click(); pg.wait_for_timeout(700)
    v, h = edge_hit('#dlg-thread')
    check('①新建话题 视口', v is True); check('①新建话题 不被盖', h is True)
    pg.locator('#dlg-cancel').click(); pg.wait_for_timeout(400)

    # 2 项目成员
    project_selector = pg.evaluate('value => ".project-chip[data-project=" + CSS.escape(value) + "]"', args.project)
    pg.locator(project_selector).first.click()
    pg.locator('#pj-tab-members').click(); pg.wait_for_timeout(700)
    v, h = edge_hit('#dlg-project-detail')
    check('②项目成员 视口', v is True); check('②项目成员 不被盖', h is True)
    pg.keyboard.press('Escape'); pg.wait_for_timeout(400)

    # 3 指定项目详情
    pg.locator(project_selector).first.click(); pg.wait_for_timeout(700)
    v, h = edge_hit('#dlg-project-detail')
    check('③项目详情 视口', v is True); check('③项目详情 不被盖', h is True)
    pg.keyboard.press('Escape'); pg.wait_for_timeout(400)

    # 4 全部项目下拉（动态 #pj-dropdown）
    pg.locator('#chip-all').click(); pg.wait_for_timeout(700)
    v, h = edge_hit('#pj-dropdown')
    check('④项目下拉 视口', v is True); check('④项目下拉 不被盖', h is True)
    pg.locator('#chip-all').click(); pg.wait_for_timeout(300)

    # 5 话题⋯菜单
    pg.locator('.tc-menu-btn').first.click(); pg.wait_for_timeout(500)
    v, h = edge_hit('.tc-menu:not([hidden])')
    check('⑤话题菜单 视口', v is True); check('⑤话题菜单 不被盖', h is True)
    pg.mouse.click(700, 400); pg.wait_for_timeout(300)

    # 6 背景交互
    check('⑥背景按钮可交互', pg.locator('#btn-new-thread').is_enabled())

    # 7 验证弹窗背景为实心，避免底层文字透出。
    a = pg.evaluate("""() => {
        const cs = getComputedStyle(document.documentElement);
        const parse = v => { const m = v.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
            const p = m[1].split(',').map(Number); return p.length > 3 ? p[3] : 1; };
        const parse2 = v => { v = v.trim(); if (v.startsWith('#')) return 'hex';
            const m = v.match(/rgba?\(([^)]+)\)/); if (!m) return null;
            const p = m[1].split(',').map(Number); return p.length > 3 ? p[3] : 1; };
        const card = parse2(cs.getPropertyValue('--card'));
        const card2 = parse2(cs.getPropertyValue('--card-2'));
        const dlg = getComputedStyle(document.querySelector('dialog'));
        // gradient 里的两个色都必须实色
        const grad = dlg.backgroundImage;
        const gradSolid = !grad.includes('rgba(') ? true :
            !grad.split('rgba(').slice(1).some(seg => { const al = parseFloat(seg.split(',')[3]); return al < 1; });
        const hasGrad = grad !== 'none';
        return {card, card2, gradSolid, hasGrad};
    }""")
    # hex 色必然实色；rgba 才查 alpha。dialog 实色判定=渐变存在 + 两变量均实色（hex 或 alpha=1）
    def solid_var(v):
        if v is None: return False
        return True if v == 'hex' else v == 1
    solid = solid_var(a['card']) and solid_var(a['card2']) and a['hasGrad'] and a['gradSolid']
    check('⑦弹窗背景实心', solid, str(a))

    print(f"\nJS错误: {len(errs)}")
    print(f"总计 {len(results)} 项，失败 {results.count(False)} 项")
    b.close()
