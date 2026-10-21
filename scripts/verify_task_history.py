"""Browser smoke test with in-memory data, no production writes or bot workers.

Run: python -B -m scripts.verify_task_history
Screenshots are saved to a temporary directory printed by this script.
"""
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright
import uvicorn

from engine import api, taskpool
from scripts.test_task_history import TaskHistoryTests


def main():
    fixture = TaskHistoryTests()
    fixture.setUp()
    fixture.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('alpha','','2026-09-23T10:00:00')")
    fixture.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('dev','沈清','fe-dev')")
    fixture.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,created_at) "
                         "VALUES(2,'alpha',1,'归档操作验证','blocked','2026-09-23T10:00:00')")
    fixture.conn.execute("INSERT INTO events(project,task_id,actor,event_type,detail,ts) "
                         "VALUES('alpha',2,'dev','block',?,'2026-09-23T10:03:00')",
                         ('缺少访问权限，请确认后恢复。\n' + 'Error detail ' * 400,))
    fixture.conn.commit()
    app = FastAPI()
    app.include_router(api.router_api)
    app.mount('/app', StaticFiles(directory='frontend', html=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    worker = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    captures = Path(tempfile.mkdtemp(prefix='task-history-ui-'))
    errors = []
    try:
        with patch.object(taskpool, '_conn', return_value=fixture.conn):
            worker.start()
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() >= deadline:
                    raise RuntimeError('isolated browser API failed to start')
                time.sleep(0.01)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                page.add_init_script("sessionStorage.setItem('ge_token','isolated-test')")
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/thinking', lambda route: route.fulfill(json={'thinking': [], 'runs': []}))
                page.goto(f'http://127.0.0.1:{sock.getsockname()[1]}/app/')
                blocked = page.locator('.task-row').filter(has_text='归档操作验证')
                expect(blocked).to_be_visible()
                blocked.hover()
                expect(blocked.locator('.tad')).to_have_css('opacity', '1')
                page.screenshot(path=str(captures / 'task-row.png'))
                checkbox = page.locator('.archive-toggle input')
                expect(checkbox).not_to_be_checked()
                assert checkbox.evaluate("e => getComputedStyle(e).backgroundColor") != 'rgb(255, 255, 255)'
                checkbox.check()
                expect(checkbox).to_be_checked()
                checkbox.uncheck()
                blocked.hover()
                blocked.get_by_role('button', name='查看', exact=True).click()
                dlg = page.locator('#dlg-task-history')
                expect(dlg).to_be_visible()
                expect(dlg).to_contain_text('缺少访问权限')
                assert dlg.evaluate('e => e.scrollWidth <= e.clientWidth'), 'long logs must wrap'
                page.screenshot(path=str(captures / 'blocked-history.png'))
                dlg.get_by_text('完整记录', exact=True).click()
                expect(dlg.locator('pre')).to_be_visible()
                assert dlg.evaluate('e => e.scrollWidth <= e.clientWidth'), 'expanded logs must wrap'
                page.get_by_role('button', name='关闭任务过程').click()
                expect(dlg).not_to_be_visible()
                history = page.locator('.task-history')
                history.locator(':scope > summary').click()
                completed = history.locator('.task-row').filter(has_text='Task')
                completed.hover()
                completed.get_by_role('button', name='查看', exact=True).click()
                expect(dlg).to_contain_text('开发交付')
                expect(dlg).to_contain_text('process exit=1')
                expect(dlg).to_contain_text('完成')
                page.screenshot(path=str(captures / 'completed-history.png'))
                page.set_viewport_size({'width': 620, 'height': 800})
                assert dlg.evaluate('e => e.getBoundingClientRect().width <= innerWidth')
                assert dlg.evaluate('e => e.scrollWidth <= e.clientWidth')
                page.keyboard.press('Escape')
                expect(dlg).not_to_be_visible()
                assert not errors, errors
                browser.close()
        print(f'PASS: task view, blocked/completed history, checkbox, long logs, narrow viewport, close/Escape. Screenshots: {captures}')
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        sock.close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
