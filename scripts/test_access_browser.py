"""Real Chromium login, SSE and reload against an isolated authenticated API."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def server():
    from unittest.mock import patch
    from scripts.unified_test_support import isolated_database, isolated_api
    with patch.dict(os.environ, {"GROUP_ADMIN_PASSWORD": "浏览器测试密码"}):
        with isolated_database(), isolated_api(frontend=True) as url:
            print(url, flush=True)
            sys.stdin.readline()


def main():
    from playwright.sync_api import sync_playwright
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    child = subprocess.Popen(
        [str(python), "-X", "utf8", "-B", __file__, "--server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        url = child.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:"), url
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "/app/")
            page.locator("#login").wait_for(state="visible")
            page.locator("#login-pwd").fill("中文错误")
            page.locator('#login-form button[type="submit"]').click()
            page.wait_for_function('document.querySelector("#login-err").textContent.includes("invalid password")')
            page.locator("#login-pwd").fill("浏览器测试密码")
            page.locator('#login-form button[type="submit"]').click()
            page.wait_for_function('document.querySelector("#conn").dataset.state === "connected"')
            assert page.locator("#login").is_hidden()
            assert context.request.get(url + "/tasks").status == 401
            page.reload()
            page.wait_for_function('document.querySelector("#conn").dataset.state === "connected"')
            assert page.evaluate('S.isUserIdentity && S.es.readyState === EventSource.OPEN')
            assert any(cookie["name"] == "group_events" and cookie["httpOnly"] for cookie in context.cookies())
            page.evaluate("logout()")
            page.locator("#login").wait_for(state="visible")
            page.wait_for_function('S.es.readyState === EventSource.CLOSED')
            for _ in range(20):
                if not any(cookie["name"] == "group_events" for cookie in context.cookies()):
                    break
                page.wait_for_timeout(100)
            assert context.request.get(url + "/events").status == 401
            assert not errors, errors
            browser.close()
        print("PASS: Unicode login, authenticated SSE, reload, API cookie isolation and logout")
    finally:
        child.communicate("\n", timeout=15)
        assert child.returncode == 0, child.returncode


if __name__ == "__main__":
    server() if "--server" in sys.argv else main()
