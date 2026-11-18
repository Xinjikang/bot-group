"""req#167 消息表双索引隔离对照（验收 2 数据源）。

在隔离库分别以「无索引 / 双索引」两配置各采 150 样本：
- reply_count：按父消息查回复数（_edge_responded 的 COUNT 查询）中位/尾延迟；
- insert_commit：单条消息插入并提交（写成本对照，确认无回归）；
- concurrent：双连接并发发送尾延迟（写锁竞争下的发送耗时，每配置 150 样本）。

输出 JSON 到 --evidence（必填绝对路径，写前检查端口无关、仅文件输出）。
不触碰生产库/引擎；临时库在系统临时目录，结束删除。
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from statistics import median

SENDER_A, SENDER_B = "sender-a", "sender-b"
SAMPLES = 150


def _p(data, q):
    s = sorted(data)
    idx = max(0, min(len(s) - 1, int(len(s) * q)))
    return s[idx]


def _mkdb(path: Path, with_indexes: bool) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL DEFAULT 'alpha',
            ts TEXT NOT NULL,
            sender TEXT NOT NULL,
            sender_type TEXT NOT NULL,
            content TEXT NOT NULL,
            mentions TEXT NOT NULL DEFAULT '[]',
            thread_id INTEGER,
            parent_id INTEGER,
            round INTEGER NOT NULL DEFAULT 0,
            depth INTEGER NOT NULL DEFAULT 0,
            retract_by TEXT,
            retract_at TEXT,
            need_reply INTEGER DEFAULT 1
        );
    """)
    if with_indexes:
        conn.executescript("""
            CREATE INDEX idx_messages_round ON messages(thread_id, sender, round, parent_id);
            CREATE INDEX idx_messages_reply ON messages(parent_id, sender);
        """)
    conn.commit()
    conn.close()


def _seed(path: Path, parents: int = 100) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = [(f"2026-11-18T10:00:{i:02d}", "user", "hello", i % 3)
            for i in range(parents)]
    conn.executemany("INSERT INTO messages (project, ts, sender, sender_type, content, "
                     "thread_id, round, depth) VALUES ('alpha', ?, 'user', 'user', ?, 1, 0, 0)",
                     [(ts, content) for ts, _, content, _ in rows])
    conn.commit()
    conn.close()


def bench_reply_count(path: Path) -> list[float]:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    lat = []
    for parent_id in range(1, SAMPLES + 1):
        t0 = time.perf_counter()
        conn.execute("SELECT COUNT(*) FROM messages WHERE parent_id=? AND sender=?",
                     (parent_id, SENDER_A)).fetchone()
        lat.append((time.perf_counter() - t0) * 1000)
    conn.close()
    return lat


def bench_insert_commit(path: Path) -> list[float]:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=30000")
    lat = []
    for i in range(SAMPLES):
        t0 = time.perf_counter()
        conn.execute("INSERT INTO messages (project, ts, sender, sender_type, content, "
                     "thread_id, round, depth) VALUES ('alpha', '2026-11-18T11:00:00', "
                     "'user', 'user', 'bench', 1, 1, 0)")
        conn.commit()
        lat.append((time.perf_counter() - t0) * 1000)
    conn.close()
    return lat


def bench_concurrent(path: Path) -> list[float]:
    """双连接并发发送尾延迟：两连接交替写（每样本一笔，采集墙钟）。"""
    lat = []
    lock = threading.Lock()

    def worker(results):
        conn = sqlite3.connect(path)  # 连接在 worker 线程内创建（sqlite3 线程约束）
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            for _ in range(SAMPLES // 2):
                t0 = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO messages (project, ts, sender, sender_type, content, "
                             "thread_id, round, depth) VALUES ('alpha', '2026-11-18T12:00:00', "
                             "'user', 'user', 'conc', 1, 2, 0)")
                conn.commit()
                with lock:
                    results.append((time.perf_counter() - t0) * 1000)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(lat,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return lat


def summarize(name: str, lat: list[float]) -> dict:
    return {"name": name, "samples": len(lat),
            "p50_ms": round(median(lat), 3),
            "p90_ms": round(_p(lat, 0.90), 3),
            "p99_ms": round(_p(lat, 0.99), 3),
            "max_ms": round(max(lat), 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, help="证据 JSON 绝对路径（新路径）")
    args = parser.parse_args()
    evidence = Path(args.evidence)
    if evidence.exists():
        print(f"证据路径已存在：{evidence}", file=sys.stderr)
        return 2
    evidence.parent.mkdir(parents=True, exist_ok=True)

    result = {"samples_per_config": SAMPLES, "configs": {}}
    with tempfile.TemporaryDirectory(prefix="send-index-bench-") as tmp:
        for label, with_indexes in (("no_index", False), ("both_indexes", True)):
            db_path = Path(tmp) / f"{label}.db"
            _mkdb(db_path, with_indexes)
            _seed(db_path)
            rows = db_path.stat().st_size
            reply = bench_reply_count(db_path)
            insert = bench_insert_commit(db_path)
            concurrent = bench_concurrent(db_path)
            result["configs"][label] = {
                "reply_count_ms": summarize("按父消息查回复数", reply),
                "insert_commit_ms": summarize("单条插入并提交", insert),
                "concurrent_send_ms": summarize("双连接并发发送", concurrent),
                "db_bytes": rows,
            }
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
