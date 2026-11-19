"""Exercise the real scanner settle path: slow selection, sends not blocked, no stale settle.

反例一浏览器补验（manager 返修③）：verify_topic_dispatch_browser 只覆盖 dispatcher
（drain/process_one），不覆盖扫描器。本脚本走真实 scanner.scan_thread_all_responded：
慢 topic_busy（5s 进程查询 I/O）期间浏览器连发两条——恢复前均获回执、按序上屏；
锁内复核发现新消息水位变化即跳过收口（话题保持 open）；记录来自 scanner 的写锁
持有时间（含提交释放），与 dispatcher 数据（verify_topic_dispatch_browser）区分。
"""
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

from engine import auth, db, router, scanner, topic_dispatch, wake
from scripts.unified_test_support import isolated_api, isolated_database


def verify(evidence):
    if not evidence.is_absolute():
        raise ValueError("--evidence requires an absolute path")
    evidence.mkdir(parents=True, exist_ok=False)
    with isolated_database(), patch.object(auth, "configured", return_value=False), isolated_api(frontend=True) as url:
        # 构造 scanner 收口候选：全员 pass、status=open、无待办（同
        # test_topic_dispatch._all_pass_open_topic 的时序）。
        with closing(db.get_connection()) as conn:
            tid = conn.execute("INSERT INTO threads(project,title,creator,members,round,created_at,last_active_at) "
                               "VALUES('alpha','扫描器收口','user','[\"user\",\"default\"]',1,'now','now')").lastrowid
            seed = router.route_message(conn, "user", "开场", "user", tid)["message_id"]
            conn.commit()
        with closing(db.get_connection()) as conn:
            assert topic_dispatch.process_one(conn)          # turn=default
            router.route_message(conn, "default", "ok", "bot", tid)
            conn.commit()
            assert topic_dispatch.process_one(conn)          # 全员 pass → settle
            conn.execute("UPDATE threads SET status='open' WHERE id=?", (tid,))
            conn.execute("DELETE FROM topic_dispatch WHERE thread_id=?", (tid,))
            conn.commit()
            assert router._next_speaker(conn, tid) is None, "收口候选成立"

        entered = threading.Event()
        resumed = threading.Event()
        real_busy = wake.topic_busy
        held_ms = []

        def slow_busy(conn, thread_id, project):
            entered.set()
            time.sleep(5)
            resumed.set()
            return real_busy(conn, thread_id, project)

        def capture_debug(_format, *args):
            if "scanner settle write lock held" in _format:
                held_ms.append(args[0])

        def run_scanner():
            # SQLite 连接必须在使用它的线程内创建
            with closing(db.get_connection()) as scanner_conn:
                scanner.scan_thread_all_responded(scanner_conn)

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
                with patch.object(wake, "topic_busy", side_effect=slow_busy), \
                        patch.object(scanner.logger, "debug", capture_debug):
                    future = pool.submit(run_scanner)
                    assert entered.wait(3), "scanner did not enter slow selection"
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
                    assert [mid for mid in visible if mid in ids] == ids, "两条按序上屏"
                    page.screenshot(path=str(evidence / "confirmed.png"))
                    future.result(timeout=10)
                # 锁内复核发现新消息 → 跳过收口：话题保持 open、两条消息在、
                # 新待办登记（不被旧快照收口吞掉）
                with closing(db.get_connection()) as conn:
                    assert conn.execute("SELECT status FROM threads WHERE id=?", (tid,)).fetchone()[0] == "open", \
                        "话题未被旧快照收口"
                    assert conn.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND sender='user'",
                                        (tid,)).fetchone()[0] == 3, "开场+两条新消息都在"
                    pending = conn.execute("SELECT COUNT(*) FROM topic_dispatch WHERE thread_id=?",
                                           (tid,)).fetchone()[0]
                    assert pending == 2, "两条新消息登记待办，不被收口吞掉"
                data = dict(pause_seconds=5, messages=samples, scanner_write_lock_ms=held_ms,
                            settled_after=False, pending_jobs=pending)
                (evidence / "result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
                print(json.dumps(data))
            finally:
                browser.close()


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    verify(parser.parse_args().evidence)
