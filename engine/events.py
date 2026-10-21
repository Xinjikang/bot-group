"""SSE 事件流：内存 pub/sub。

写操作（发消息/建话题/收口/修订/队列状态变化）广播事件，SSE 端点订阅。
单进程内存实现，够用（引擎单实例）。事件类型：
- message.new / thread.created / thread.closed / task.updated / wake.changed
- wake.spawned（引擎 spawn bot 成功，2026-09-10 思考条 SSE 化）
- thinking.changed（心跳上线/下线，2026-09-10 思考条 SSE 化）
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """主事件循环注入（main.lifespan 调），后台线程发事件走 threadsafe 通道。"""
    global _loop
    _loop = loop


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """async 请求上下文内直接调；后台线程（reaper/心跳线程）也可调——
        内部经 call_soon_threadsafe 切回主 loop，asyncio.Queue 竞态安全。
        loop 未注入时（引擎启动早期）静默丢弃。"""
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        loop = _loop
        if loop is None or loop.is_closed():
            return
        subs = list(self._subscribers)  # 快照，防迭代中修改

        def _deliver() -> None:
            for q in subs:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # 订阅者消费太慢，丢弃最旧一条再塞
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:  # noqa: BLE001
                        pass

        try:
            loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            # loop 正在关闭
            pass


bus = EventBus()