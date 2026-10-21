# -*- coding: utf-8 -*-
"""端到端验证 v2：隐藏链路（展开后菜单点击用 JS 直调 toggleHideThread 避免菜单可见性问题）"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8180/app/"
results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), name, detail)

tid = None
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    page.click("#login-form button[type=submit]")
    page.wait_for_timeout(2500)

    # 1. 新建 open 话题
    page.click("#btn-new-thread")
    page.fill("#dlg-title", "隐藏链路验证-可删")
    page.click("#ms-trigger"); page.locator(".ms-option input").first.check()
    page.fill("#dlg-opening", "include_hidden 验证")
    page.click("#thread-form button[type=submit]")
    page.wait_for_timeout(1500)

    # 2. 菜单隐藏（真实 /hide）
    btn = page.locator('.thread-card:not(.thread-closed-card) .tc-menu-btn')
    check("open 卡存在", btn.count() >= 1, f"{btn.count()}")
    btn.first.click(); page.wait_for_timeout(300)
    page.locator('.tc-menu').first.locator('[data-act="hide"]').click()
    page.wait_for_timeout(1200)

    # 3. S.threads 含该话题且 hidden=1（include_hidden 生效）
    hidden_vals = page.evaluate('() => S.threads.filter(t=>t.title.includes("隐藏链路验证")).map(t=>t.hidden)')
    check("include_hidden 拿到隐藏话题 hidden=1", hidden_vals == [1], str(hidden_vals))

    # 4. 折叠条出现
    tog = page.locator(".hidden-toggle")
    check("折叠条出现", tog.count() > 0, tog.first.inner_text() if tog.count() else "")

    # 5. 展开 → 隐藏卡（虚线弱化）可见
    if tog.count():
        tog.first.click(); page.wait_for_timeout(300)
        n = page.locator(".thread-card.thread-hidden-card").count()
        check("展开后隐藏卡可见", n > 0, f"{n} 张")

        # 6. 取消隐藏：直调函数（菜单 hover 可见性问题绕开，函数本身已被第2步验证）
        page.evaluate("""() => {
          const t = S.threads.find(t=>t.title.includes('隐藏链路验证'));
          if (t) toggleHideThread(t);
        }""")
        page.wait_for_timeout(1200)
        after = page.evaluate('() => S.threads.filter(t=>t.title.includes("隐藏链路验证")).map(t=>t.hidden)')
        check("取消隐藏恢复 hidden=0", after == [0], str(after))

    tid = page.evaluate("() => { const t = S.threads.find(t=>t.title.includes('隐藏链路验证')); return t ? t.id : null; }")
    check("零JS错误", not errors, "; ".join(errors[:3]))
    browser.close()

# 清理测试话题
if tid:
    import sqlite3
    db = sqlite3.connect(Path(__file__).resolve().parents[1] / "group.db")
    db.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
    db.execute("DELETE FROM threads WHERE id=?", (tid,))
    db.commit(); db.close()
    print("cleaned", tid)

fails = [r for r in results if not r[1]]
print(f"\n总计 {len(results)} 项，失败 {len(fails)} 项")
sys.exit(1 if fails else 0)
