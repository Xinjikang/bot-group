"""test_gate_reason_frontend.py — req#111 task#191 前端标注测试（真实 Chromium 合成接口）。

覆盖验收三条：
①需求面板对未过拆解门禁的需求标「待拆解授权」+具体原因（列表空态行 + 详情弹窗行）；
②已过门禁需求（gate_reason 空不误标，正常显示「需求已创建，等待拆分任务」）；
③原因文案来自后端（mock /tasks requirements[].gate_reason 与 /requirements/{id} resp；
  后端落库/清空契约由 scripts/test_requirement_gate_reason.py 覆盖，此处前端纯展示）。

运行：python -B scripts/test_gate_reason_frontend.py
"""
import mimetypes
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]

# req#111 修拆解门禁 fail-late（gate_reason 非空=被拦）+ req#110 已过门禁对照
REQUIREMENTS = [
    {"id": 111, "project": "p1", "title": "修复 req 拆解门禁 fail-late", "status": "open",
     "archived": 0, "version": "",
     "gate_reason": "来源信息无效：source_ref 缺 thread_id/message_id（格式：thread_id=…; message_id=…）"},
    {"id": 110, "project": "p1", "title": "网页内目录选择器替代原生对话框", "status": "open",
     "archived": 0, "version": "", "gate_reason": ""},
]
TASKS = []  # 两需求都零任务 → 空态行；#111 被门禁拦、#110 正常

def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(getattr(e, "stack", None) or str(e)))

        def route(request):
            path = request.request.url.split("?")[0]
            if "/app/" in path:
                f = ROOT / "frontend" / path.split("/app/")[1]
                request.fulfill(body=f.read_bytes(),
                                content_type=mimetypes.guess_type(f.name)[0] or "text/plain")
                return
            if path.endswith("/tasks"):
                request.fulfill(json={"requirements": REQUIREMENTS, "tasks": TASKS,
                                      "projects": [], "members": [], "threads": [], "messages": []})
                return
            if "/requirements/111" in path:
                request.fulfill(json={"requirement": REQUIREMENTS[0], "tasks": []})
                return
            request.fulfill(json={"projects": [], "members": [], "threads": [], "messages": []})

        page.route("**/*", route)
        page.goto("http://workspace.test/app/index.html")
        page.evaluate("showLogin(false)")
        page.wait_for_function("document.querySelector('#task-list')")
        page.evaluate("loadTasks()")
        page.wait_for_function("document.querySelector('.req-block')")

        # —— ① 需求 #111（gate_reason 非空）：空态行标「待拆解授权 · 原因」+ warn 色 ——
        block = page.locator(".req-block", has_text="修复 req 拆解门禁 fail-late").first
        block.locator("summary").click()
        gate_row = page.locator(".req-gate-blocked").first
        page.wait_for_function(
            "document.querySelector('.req-gate-blocked') && "
            "document.querySelector('.req-gate-blocked').textContent.includes('待拆解授权')")
        txt = gate_row.evaluate("el => el.textContent")
        assert "待拆解授权 · 来源信息无效" in txt and "thread_id" in txt, f"①空态行文案不对：{txt}"
        color = gate_row.evaluate("el => getComputedStyle(el).color")
        assert color != "rgb(0, 0, 0)", f"①warn 色未生效：{color}"

        # —— ② 需求 #110（已过门禁 gate_reason=''）：不误标，仍显示「等待拆分任务」——
        ok_block = page.locator(".req-block", has_text="网页内目录选择器").first
        assert ok_block.locator(".req-gate-blocked").count() == 0, "②已过门禁不应有 gate 标注"
        assert "等待拆分任务" in ok_block.inner_text(), "②已过门禁空态行应保持原文案"

        # —— ③ 原因文案来自后端响应：详情弹窗再验一遍（gate_reason 原样透传）——
        block.locator("summary").click()  # 回到 #111 卡片再点查看（ok_block 的查看开的是 #110 无 gate）
        block.locator(".req-view-btn", has_text="查看").first.click()
        page.wait_for_function("document.querySelector('#dlg-requirement-details').open")
        gate_p = page.locator("[data-requirement-gate='blocked']")
        page.wait_for_function(
            "document.querySelector(\"[data-requirement-gate='blocked']\") && "
            "document.querySelector(\"[data-requirement-gate='blocked']\").textContent.includes('待拆解授权')")
        dtxt = gate_p.evaluate("el => el.textContent")
        assert "待拆解授权 · 来源信息无效" in dtxt and "message_id" in dtxt, f"③详情行文案不对：{dtxt}"
        assert not errors, f"页面错误：{errors[:2]}"
        browser.close()

    print("PASS: gate reason banner (req#111 #191) list empty-state + details dialog, pass-through, no false tag")

if __name__ == "__main__":
    main()