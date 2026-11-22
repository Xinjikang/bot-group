"""Exercise the real composer, HTTP send/read endpoints and SQLite dispatcher."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from engine import auth, db, router, topic_dispatch
from scripts.unified_test_support import isolated_api, isolated_database


def verify(evidence):
    if not evidence.is_absolute():
        raise ValueError("--evidence requires an absolute path")
    evidence.mkdir(parents=True, exist_ok=False)
    with isolated_database(), patch.object(auth, "configured", return_value=False), isolated_api(frontend=True) as url:
        with closing(db.get_connection()) as conn:
            tid = conn.execute("INSERT INTO threads(project,title,creator,members,round,created_at,last_active_at) "
                               """VALUES('alpha','异步发送','user','["user","default"]',1,'now','now')""").lastrowid
            seed = router.route_message(conn, "user", "开始选人", "user", tid)["message_id"]

        entered = threading.Event()
        resumed = threading.Event()
        select = router._next_speaker
        write_ms = []
        order = []

        def delayed(conn, topic, through):
            order.append(through)
            if through == seed:
                entered.set()
                time.sleep(5)
                resumed.set()
            return select(conn, topic, through)

        def drain():
            with closing(db.get_connection()) as worker:
                while topic_dispatch.process_one(worker):
                    pass

        def capture(_format, _tid, _mid, _read, write):
            write_ms.append(write)

        def route(req):
            path = urlsplit(req.request.url).path
            if path.startswith("/app/") or path in ("/msg", "/messages", "/threads", "/thread"):
                return req.continue_()
            if path == "/events":
                return req.fulfill(body=": fixture\n\n", content_type="text/event-stream")
            data = {"/auth/status": {"configured": False},
                    "/projects": {"projects": [{"name": "alpha", "display_name": "隔离项目"}]},
                    "/members": {"members": [{"profile": "default", "active": 1}]},
                    "/tasks": {"tasks": [], "requirements": [], "pending_approval": []}}
            req.fulfill(json=data.get(path, {}))

        with sync_playwright() as pw, ThreadPoolExecutor(max_workers=1) as pool:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", route)
                page.goto(url + "/app/")
                page.wait_for_function("S.messagesReady")
                page.evaluate("async id => { S.currentProject='alpha'; await selectThread(id); }", tid)
                page.locator(f'[data-message-id="{seed}"]').wait_for()
                samples = []
                with patch.object(router, "_next_speaker", delayed), patch.object(topic_dispatch.logger, "debug", capture):
                    future = pool.submit(drain)
                    assert entered.wait(3), "worker did not enter selection"
                    for text in ("暂停期间第一条", "暂停期间第二条"):
                        composer = page.locator("#composer-text")
                        composer.fill(text)
                        start = time.perf_counter()
                        with page.expect_response(lambda r: urlsplit(r.url).path == "/msg") as result:
                            composer.press("Enter")
                        resp = result.value
                        assert resp.ok, resp.status
                        mid = resp.json()["message_id"]
                        row = page.locator(f'[data-message-id="{mid}"]')
                        row.get_by_text("已发送", exact=True).wait_for(timeout=2000)
                        assert text in row.inner_text() and not resumed.is_set()
                        samples.append({"id": mid, "http_ms": resp.request.timing["responseEnd"],
                                        "visible_ms": (time.perf_counter() - start) * 1000})
                    ids = [s["id"] for s in samples]
                    visible = page.locator("[data-message-id]").evaluate_all(
                        "(nodes) => nodes.map(n => Number(n.dataset.messageId))")
                    assert [mid for mid in visible if mid in ids] == ids
                    page.screenshot(path=str(evidence / "confirmed.png"))
                    future.result(timeout=10)
                assert order == [seed, *ids] and len(write_ms) == 3
                assert max(write_ms) < 100 and all(0 <= s["http_ms"] < 500 for s in samples)
                data = dict(pause_seconds=5, messages=samples, selection_order=order, write_ms=write_ms)
                (evidence / "result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
                print(json.dumps(data))
            finally:
                browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    verify(parser.parse_args().evidence)
