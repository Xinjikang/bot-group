"""group-engine 常驻服务入口。

职责（ENGINEERING.md 第 2 节架构图）：
- FastAPI app（:8180）：api.py 的路由挂载点
- 后台任务：queue.py 串行队列 worker、scanner.py 周期扫描器（180s，req#30）
- 启动时 db.py 建表（schema.sql，WAL + busy_timeout=30s）、running job 回 queued（重启恢复）

启动：python -m engine.main
"""

from __future__ import annotations

import logging
import logging.handlers
import asyncio
import os
import threading
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request

from . import db
from . import config
from .api import router_api, _resolve_identity, _admin_only
from . import queue, scanner
from pathlib import Path
from fastapi.staticfiles import StaticFiles

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# 日志落数据目录（req#122 程序/数据分离）；pythonw 无窗口时 stdout 不存在，必须写文件才能排障
logger = logging.getLogger("group-engine")


def _setup_logging() -> None:
    """日志落数据目录，随 --data-dir 定位（main 启动 apply_cli 后调用，见 main()）。

    pythonw 无窗口时 stdout 不存在，必须写文件才能排障；5MB 轮转防常驻单文件膨胀。
    """
    log_dir = config.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),   # 交互启动时仍打屏幕
            logging.handlers.RotatingFileHandler(  # 5MB 轮转，防常驻数月后单文件涨到几百 MB
                log_dir / "engine.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ],
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import wake
    wake._shutting_down.clear()
    # 启动：建表 + 重启恢复
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        recovered = db.recover_on_startup(conn)
        if recovered:
            logger.info("重启恢复：%d 个投递或话题执行重新入队", recovered)
    finally:
        conn.close()

    stop_event = threading.Event()
    app.state.stop_event = stop_event
    app.state.instance_id = uuid.uuid4().hex
    from .lifecycle import Shutdown
    server = getattr(app.state, 'server', None)
    app.state.shutdown = Shutdown(stop_event, lambda: setattr(server, 'should_exit', True))
    from .restart import Restart
    app.state.restart = Restart(app.state.instance_id)

    # SSE 跨线程投递：后台线程（reaper/心跳线程/queue worker）发事件要 threadsafe 切回主 loop
    from . import events
    events.set_loop(asyncio.get_running_loop())
    from . import hermes_gateway
    hermes_gateway.recover()

    t_queue = threading.Thread(target=queue.run_forever, args=(stop_event,), daemon=True, name="queue-worker")
    t_scanner = threading.Thread(target=scanner.run_forever, args=(stop_event,), daemon=True, name="scanner")
    t_queue.start()
    t_scanner.start()
    logger.info("后台线程已启动：queue-worker + scanner")

    try:
        yield
    finally:
        wake._shutting_down.set()
        stop_event.set()
        await asyncio.to_thread(t_queue.join, 5)
        await asyncio.to_thread(t_scanner.join, 5)
        # The managed server drains while HTTP/MCP is still available, before
        # Uvicorn closes connections. Keep a fallback for embedded ASGI hosts.
        if not wake.active_runs():
            from . import browser_resources
            result = await asyncio.to_thread(browser_resources.release)
            if not result['ok']:
                logger.error('Browser resources remain: %s', result['errors'])


app = FastAPI(title="group-engine", version="0.1.0", lifespan=lifespan)
app.include_router(router_api)
from .ui_presence import router as ui_presence_router
app.include_router(ui_presence_router)

# 静态前端挂在 /app（访问 http://127.0.0.1:8180/app/）
if _FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


@app.get("/health")
def health(request: Request):
    result = {"status": "ok", "pid": os.getpid(),
            "instance_id": getattr(app.state, 'instance_id', ''), 'graceful_shutdown': 1,
            }
    try:
        identity = _resolve_identity(request.headers.get("x-group-profile"),
                                     request.headers.get("authorization"),
                                     request.headers.get("x-group-key"), request)
        _admin_only(identity, "Administrator access required")
    except HTTPException:
        return result
    return {**result, 'program_root': str(config.program_root().resolve()),
            'db_path': str(Path(config.db_path()).resolve())}


@app.post('/admin/shutdown')
def request_shutdown(identity=Depends(_resolve_identity)):
    _admin_only(identity, '退出引擎仅限管理员')
    if getattr(app.state, 'server', None) is None:
        raise HTTPException(503, '请通过 python -m engine.main 启动以启用优雅退出')
    return {**app.state.shutdown.begin(), 'instance_id': app.state.instance_id}


@app.get('/admin/shutdown')
def shutdown_status(identity=Depends(_resolve_identity)):
    _admin_only(identity, '退出状态仅限管理员')
    return {**app.state.shutdown.snapshot(), 'instance_id': app.state.instance_id}


@app.post('/admin/restart', status_code=202)
def request_restart(identity=Depends(_resolve_identity)):
    _admin_only(identity, '重启服务仅限管理员')
    if getattr(app.state, 'server', None) is None:
        raise HTTPException(503, '请通过 python -m engine.main 启动服务')
    try:
        return app.state.restart.begin()
    except (OSError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get('/admin/restart')
def restart_status(identity=Depends(_resolve_identity)):
    _admin_only(identity, '重启状态仅限管理员')
    from . import restart
    return {**restart.status(), 'shutdown': app.state.shutdown.snapshot()}


class ManagedServer(uvicorn.Server):
    def handle_exit(self, sig, frame):
        shutdown = getattr(app.state, 'shutdown', None)
        if shutdown is None:
            super().handle_exit(sig, frame)
        else:
            shutdown.begin()


def main(argv=None) -> None:
    import argparse
    parser = argparse.ArgumentParser(description="group-engine 常驻服务")
    parser.add_argument('--host', default=None, help='监听地址（默认 127.0.0.1 回环）')
    parser.add_argument('--port', type=int, default=None, help='端口（默认 8180）')
    parser.add_argument('--data-dir', default=None,
                        help='数据目录（默认程序根目录；可通过 GROUP_DATA_DIR 或 DATA_DIR 指定）')
    args = parser.parse_args(argv)
    # 应用命令行覆盖到模块级状态，使 wake._engine_url()/mcp 回调派生 engine_url 时读到 --host/--port
    config.apply_cli(host=args.host, port=args.port, data_dir=args.data_dir)
    # 命令行数据目录固定本次进程，db/attachments/日志均按此定位（日志目录随 --data-dir，不再 import 期固定）
    if args.data_dir:
        os.environ['GROUP_DATA_DIR'] = args.data_dir
    _setup_logging()
    server = ManagedServer(uvicorn.Config(app, host=config.host(args.host),
        port=config.port(args.port), log_level='info',
        timeout_graceful_shutdown=10))
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
