"""Drain executions and release browser resources before asking Uvicorn to exit."""
import logging
import threading
import time

from . import browser_resources, db, wake

logger = logging.getLogger(__name__)


class Shutdown:
    def __init__(self, stop_event, exit_server, timeout=120):
        self.stop_event = stop_event
        self.exit_server = exit_server
        self.timeout = timeout
        self.lock = threading.Lock()
        self.state = 'running'
        self.error = ''
        self.worker = None

    def snapshot(self):
        with self.lock:
            return {'state': self.state, 'error': self.error}

    def begin(self):
        with self.lock:
            if self.state in ('draining', 'terminating', 'releasing', 'stopping'):
                return {'state': self.state, 'error': self.error}
            self.state, self.error = 'draining', ''
            self.worker = threading.Thread(target=self._run, name='engine-shutdown', daemon=True)
            self.worker.start()
            return {'state': self.state, 'error': self.error}

    def _runs(self):
        # Includes executions recovered from a previous interrupted engine.
        conn = db.get_connection()
        try:
            rows = conn.execute('SELECT r.* FROM wake_runs r '
                                'WHERE (r.ended_at IS NULL OR r.exit_code=-1) AND r.started_at='
                                '(SELECT MAX(w.started_at) FROM wake_runs w WHERE w.session=r.session)').fetchall()
        finally:
            conn.close()
        return rows

    def _busy(self):
        from . import hermes_gateway
        # gateway 托管未终态执行不阻塞引擎退出（设计 4.4：重启不等 bot 终态）。
        managed = hermes_gateway.managed_runs()
        for run in wake.active_runs():
            if (run.get('session'), run.get('wake_started_at')) not in managed:
                return True
        rows = self._runs()
        return any(
            (r['session'], r['started_at']) not in managed
            and wake.find_bot_processes(r['profile'], session=r['session']) != []
            for r in rows
        )

    def _terminate(self):
        from . import process_tree, heartbeat, hermes_gateway
        rows = self._runs()
        managed = hermes_gateway.managed_runs()
        # gateway 托管未终态执行不取消、不终止（bot 继续运行，设计 4.4）；
        # 本地 viewer 连接关闭在 _run 的 release 前统一执行。
        local_rows = [r for r in rows if (r['session'], r['started_at']) not in managed]
        pids = set()
        launchers = {}
        for row in local_rows:
            found = wake.find_bot_processes(row['profile'], session=row['session'])
            if found is None:
                raise RuntimeError(f"无法核实 Bot 进程：{row['profile']}")
            wake.mark_intentional_stop(row['profile'], row['session'])
            pids.update(found)
            launchers.update({pid: row['pid'] or pid for pid in found})
        if pids:
            process_tree.terminate(pids, launchers=launchers)
        # Reapers own the normal execution bookkeeping; keep HTTP/MCP available
        # until they have flushed exit state. Recovered executions have no reaper.
        deadline = time.monotonic() + 15
        while self._busy():
            if time.monotonic() >= deadline:
                raise RuntimeError('停止 Bot 后仍有执行未完成收尾，请查看引擎日志')
            time.sleep(0.1)
        conn = db.get_connection()
        try:
            for row in local_rows:
                conn.execute('UPDATE wake_runs SET ended_at=?,exit_code=1 '
                             'WHERE session=? AND started_at=? AND (ended_at IS NULL OR exit_code=-1)',
                             (time.time(), row['session'], row['started_at']))
            conn.commit()
        finally:
            conn.close()
        for row in local_rows:
            if row['pid']:
                heartbeat.on_exit(row['profile'], pid=row['pid'], thread_id=row['thread_id'])

    def _run(self):
        try:
            from .project_workspaces import LOCK
            from .session_adapter import _SWITCH_LOCK
            # Serialize the gate with spawn, including publication of active_runs.
            with LOCK, _SWITCH_LOCK:
                wake._shutting_down.set()
            deadline = time.monotonic() + self.timeout
            while self._busy():
                if time.monotonic() >= deadline:
                    with self.lock:
                        self.state = 'terminating'
                    self._terminate()
                    break
                time.sleep(0.1)
            with self.lock:
                self.state = 'releasing'
            # 本地收尾：有界关闭 gateway viewer 连接（硬超时 5s），不阻塞引擎退出（设计 4.4/6.1）。
            from . import hermes_gateway
            hermes_gateway.close_connections(timeout=5)
            result = browser_resources.release(exclude_managed=True)
            if not result['ok']:
                raise RuntimeError('浏览器资源释放失败，请重试退出；详情见引擎日志')
            with self.lock:
                self.state = 'stopping'
            self.exit_server()
            self.stop_event.set()
        except Exception as exc:
            logger.exception('Graceful engine shutdown failed')
            # Workers stay alive during drain. Reopen the spawn gate only when
            # the application itself has not started its final teardown.
            with LOCK, _SWITCH_LOCK, self.lock:
                if not self.stop_event.is_set():
                    from .hermes_gateway_client import resume_connections
                    resume_connections()
                    wake._shutting_down.clear()
                self.state, self.error = 'failed', str(exc)
