"""Real Chromium notification tests using only local files and synthetic input.

Run: python scripts/test_alert_dialog.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 600})
        native = []
        page.on("dialog", lambda d: (native.append(d.message), d.dismiss()))
        html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        styles = html.split("<style>", 1)[1].split("</style>", 1)[0]
        page.set_content('<style>' + styles + '</style><button id="origin">Open</button>')
        page.add_style_tag(path=str(ROOT / "frontend/dialogs.css"))
        page.add_script_tag(path=str(ROOT / "frontend/dialogs.js"))
        page.locator("#origin").focus()
        page.evaluate(r"""() => {
          window.dismissedAlerts = [];
          showAlert('<img src=x onerror=alert(1)>\nSecond line').then(() => dismissedAlerts.push(1));
          showAlert('Queued message').then(() => dismissedAlerts.push(2));
        }""")
        dialog = page.locator("#app-alert")
        assert dialog.is_visible()
        assert page.locator("#app-alert-message img").count() == 0
        assert page.locator("#app-alert-message").inner_text().endswith("Second line")
        assert page.locator("#app-alert .primary").evaluate("e => e === document.activeElement")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#app-alert-message').textContent === 'Queued message'")
        assert page.evaluate("dismissedAlerts") == [1]
        page.keyboard.press("Escape")
        page.wait_for_function("dismissedAlerts.length === 2")
        assert page.locator("#origin").evaluate("e => e === document.activeElement")
        # Stack above an existing modal and restore its input focus.
        page.evaluate(r"""() => {
          const parent = document.createElement('dialog'); parent.id = 'parent';
          parent.innerHTML = '<input id="parent-input">'; document.body.append(parent);
          parent.showModal(); document.querySelector('#parent-input').focus();
          showAlert('x'.repeat(2000) + '\nline'.repeat(200));
        }""")
        box = dialog.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390
        assert box["y"] >= 0 and box["height"] <= 600
        page.locator("#app-alert .primary").click()
        page.wait_for_function("!document.querySelector('#app-alert').open")
        assert page.locator("#parent").evaluate("e => e.open")
        assert page.locator("#parent-input").evaluate("e => e === document.activeElement")
        page.evaluate("document.querySelector('#parent').close()")
        # Exercise the actual attachment validation call site.
        page.add_script_tag(path=str(ROOT / "frontend/attachments.js"))
        page.evaluate("""() => {
          const container = document.createElement('div');
          const input = document.createElement('textarea');
          container.append(input); document.body.append(container);
          createImageComposer(container, input, () => ({project: ''}));
          container.querySelector('input[type=file]').dispatchEvent(new Event('change'));
        }""")
        assert page.locator("#app-alert-message").inner_text() == "请先选择项目"
        page.locator("#app-alert .primary").click()
        page.wait_for_function("!document.querySelector('#app-alert').open")
        # Exercise the screenshot's actual archive handler with a fake API.
        start = html.index('async function archiveRequirement(')
        end = html.index('async function adminTaskAction(', start)
        page.evaluate("window.apiCalls = []; window.api = async (...args) => apiCalls.push(args); window.loadTasks = () => {}")
        page.add_script_tag(content=html[start:end])
        page.evaluate("() => { window.archiveDone = false; archiveRequirement(40, true, 'demo').then(() => archiveDone = true); }")
        assert page.evaluate("apiCalls.length") == 0
        # req#106 #184：showConfirm 走 enqueue→showNext 异步队列，消息渲染在 showNext 内完成——
        # 此处直接同步 assert 是时序 flaky（消息未写入 DOM 时偶发失败），改为确定性等待。
        page.wait_for_function("document.querySelector('#app-alert-message')?.textContent.includes('需求 #40')")
        page.get_by_role('button', name='取消', exact=True).click()
        page.wait_for_function('archiveDone')
        assert page.evaluate('apiCalls.length') == 0
        page.evaluate("() => { window.archiveDone = false; archiveRequirement(40, true, 'demo').then(() => archiveDone = true); }")
        page.locator('#app-alert .primary').click()
        page.wait_for_function('archiveDone')
        assert page.evaluate('apiCalls[0][0]') == '/admin/requirement/40/archive'
        assert page.evaluate('JSON.parse(apiCalls[0][1].body).project') == 'demo'
        # Escape cancels even after a prior confirmed dialog; prompt preserves empty input.
        page.evaluate("() => { window.answer = 'pending'; showConfirm('Cancel me').then(v => answer = v); }")
        page.keyboard.press('Escape')
        page.wait_for_function('answer === false')
        page.evaluate("() => { window.answer = 'pending'; showPrompt('Conclusion').then(v => answer = v); }")
        page.keyboard.press('Enter')
        page.wait_for_function("answer === ''")
        page.evaluate("() => { window.answer = 'pending'; showPrompt('Conclusion', 'draft').then(v => answer = v); }")
        page.keyboard.press('Escape')
        page.wait_for_function('answer === null')
        page.evaluate("() => { window.answer = 'pending'; showPrompt('Conclusion').then(v => answer = v); }")
        page.locator('#app-alert input').fill('Completed')
        page.keyboard.press('Enter')
        page.wait_for_function("answer === 'Completed'")
        assert not native, native
        browser.close()
    print("PASS: queue, keyboard, focus, layout, safe text, attachments, archive confirm/cancel API gate, prompt text/empty/cancel")


if __name__ == "__main__":
    main()
