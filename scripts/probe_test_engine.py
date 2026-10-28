"""#253 隔离测试引擎（task_queue 最小替身），仅供探针 MCP 调用。

作用：给测试 MCP 一个"引擎地址"，用于验证「MCP 注入的引擎地址只指向隔离实例」。
- 只监听 127.0.0.1 的随机端口（启动时 port=0，由内核分配后写 ready 文件）；
- 数据落在调用方指定的 SQLite 文件（隔离 test-data），每次请求记一行，可用于核对
  "MCP 调用的确实是这个隔离实例"；
- 只提供 /health、/whoami、/api/projects 三个只读接口，没有任何写库或生产访问能力。

不是项目引擎实例：项目引擎的 bot_token 每次启动随机、MCP 还要先登录取 token，
与本任务要验证的网关侧行为（MCP 注入、冻结、归属）无关，故用最小替身并在报告中注明。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path(os.environ["PROBE_ENGINE_DB"]).resolve()
READY_PATH = Path(os.environ["PROBE_ENGINE_READY"]).resolve()
_LOCK = threading.Lock()


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hits (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts TEXT DEFAULT CURRENT_TIMESTAMP, path TEXT, agent TEXT, tag TEXT)")
        conn.commit()


def _record(path: str, agent: str, tag: str) -> None:
    with _LOCK, sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO hits(path, agent, tag) VALUES (?,?,?)", (path, agent, tag))
        conn.commit()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 不写 stderr，避免污染 MCP 侧日志
        pass

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        agent = self.headers.get("X-Probe-Agent", "")
        tag = self.headers.get("X-Probe-Tag", "")
        _record(self.path, agent, tag)
        if self.path.startswith("/health"):
            self._json({"ok": True, "pid": os.getpid(), "port": self.server.server_port,
                        "db": str(DB_PATH), "isolated": True})
        elif self.path.startswith("/whoami"):
            self._json({"agent": agent, "tag": tag, "pid": os.getpid(),
                        "port": self.server.server_port, "db": str(DB_PATH),
                        "hermes_home": os.environ.get("HERMES_HOME")})
        elif self.path.startswith("/api/projects"):
            self._json({"projects": [], "isolated": True, "pid": os.getpid()})
        else:
            self._json({"error": "not_found", "path": self.path}, 404)


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    READY_PATH.write_text(json.dumps({
        "pid": os.getpid(), "port": server.server_port, "db": str(DB_PATH),
        "url": f"http://127.0.0.1:{server.server_port}"}), encoding="utf-8")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
